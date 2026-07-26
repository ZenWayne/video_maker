"""COS 对象操作原语。不含任何业务语义。

SDK 是纯同步的，因此每个传输方法都用 asyncio.to_thread 把阻塞调用移出
事件循环。唯一例外是 signed_url —— 预签名是纯本地 HMAC 计算，不发网络
请求，进线程池只会徒增开销。

一致性约定（调用方须遵守）：新增文件先 put 成功再写 DB；删除先改 DB 解除
引用再删 COS。DB 中的 key 必须永远指向真实存在的对象，宁可留孤儿对象。
"""

import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import Optional

from app.config import settings
from app.services.cos_client import (
    bucket,
    credentials_remaining_sec,
    get_client,
)

logger = logging.getLogger(__name__)

# 超过此大小走 upload_file 高级接口（自动分块 + 断点续传）
MULTIPART_THRESHOLD = 100 * 1024 * 1024


def _log_cos_error(op: str, key: str, exc: Exception) -> None:
    """记录 RequestId —— 腾讯云工单排查以它为准。"""
    logger.error(
        "cos_operation_failed",
        extra={
            "op": op,
            "key": key,
            "error_code": getattr(exc, "get_error_code", lambda: None)(),
            "status_code": getattr(exc, "get_status_code", lambda: None)(),
            "request_id": getattr(exc, "get_request_id", lambda: None)(),
        },
    )


async def put(key: str, local_path: Path, content_type: Optional[str] = None) -> str:
    """上传本地文件到 key。返回 key。"""
    local_path = Path(local_path)
    n = local_path.stat().st_size
    ct = content_type or mimetypes.guess_type(local_path.name)[0]
    client = get_client()

    def _do():
        if n >= MULTIPART_THRESHOLD:
            # 高级接口按大小自动选简单/分块上传，分块自带断点续传。
            # ContentType 经 **kwargs 透传给 create_multipart_upload（内部
            # mapped() 会把它映射成 Content-Type 头）——读路径是浏览器
            # <video> 直接播签名 URL，缺失/错误的 Content-Type 会让浏览器
            # 改成下载而非内联播放，成片越过阈值时尤其容易踩中。
            kwargs = {
                "Bucket": bucket(), "Key": key, "LocalFilePath": str(local_path),
                "PartSize": 10, "MAXThread": 10, "EnableMD5": False,
            }
            if ct:
                kwargs["ContentType"] = ct
            return client.upload_file(**kwargs)
        kwargs = {"Bucket": bucket(), "Key": key, "EnableMD5": False}
        if ct:
            kwargs["ContentType"] = ct
        with open(local_path, "rb") as f:
            return client.put_object(Body=f, **kwargs)

    try:
        await asyncio.to_thread(_do)
    except Exception as e:
        _log_cos_error("put", key, e)
        raise

    logger.info("cos_put", extra={"key": key, "bytes": n})
    return key


async def get(key: str, dest_path: Path) -> Path:
    """下载 key 到本地。父目录自动创建。返回 dest_path。"""
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    client = get_client()

    def _do():
        r = client.get_object(Bucket=bucket(), Key=key)
        # 流式写盘，不整体读入内存：分镜视频可达数百 MB
        r["Body"].get_stream_to_file(str(dest_path))

    try:
        await asyncio.to_thread(_do)
    except Exception as e:
        _log_cos_error("get", key, e)
        raise
    logger.info("cos_get", extra={"key": key, "bytes": dest_path.stat().st_size})
    return dest_path


async def exists(key: str) -> bool:
    """对象是否存在。用 SDK 自带的 object_exists。"""
    client = get_client()
    try:
        return await asyncio.to_thread(client.object_exists, bucket(), key)
    except Exception as e:
        _log_cos_error("exists", key, e)
        raise


async def size(key: str) -> int:
    """对象字节数。不存在时抛 FileNotFoundError。"""
    client = get_client()
    try:
        r = await asyncio.to_thread(client.head_object, Bucket=bucket(), Key=key)
    except Exception as e:
        if getattr(e, "get_status_code", lambda: None)() == 404:
            raise FileNotFoundError(key) from e
        _log_cos_error("size", key, e)
        raise
    return int(r["Content-Length"])


async def copy(src_key: str, dst_key: str) -> str:
    """服务端拷贝，不产生本地流量。返回 dst_key。"""
    client = get_client()
    source = {
        "Bucket": bucket(),
        "Key": src_key,
        "Region": settings.cos_region,
    }
    try:
        await asyncio.to_thread(
            client.copy_object, Bucket=bucket(), Key=dst_key, CopySource=source
        )
    except Exception as e:
        _log_cos_error("copy", src_key, e)
        raise
    logger.info("cos_copy", extra={"src": src_key, "dst": dst_key})
    return dst_key


async def delete(key: str) -> None:
    """删除对象。幂等——对象不存在不报错。"""
    client = get_client()
    try:
        await asyncio.to_thread(client.delete_object, Bucket=bucket(), Key=key)
    except Exception as e:
        if getattr(e, "get_status_code", lambda: None)() == 404:
            return
        _log_cos_error("delete", key, e)
        raise
    logger.info("cos_delete", extra={"key": key})


def _list_prefix_sync(client, prefix: str, max_keys: Optional[int] = None) -> list[str]:
    """分页取全前缀下所有 key。同步阻塞——调用方须经 asyncio.to_thread。

    ``max_keys`` 仅供测试用小页强制触发真实分页；生产路径不传，用 SDK
    默认（1000/页）。
    """
    keys: list[str] = []
    marker = ""
    while True:
        kwargs = {"Bucket": bucket(), "Prefix": prefix, "Marker": marker}
        if max_keys is not None:
            kwargs["MaxKeys"] = max_keys
        r = client.list_objects(**kwargs)
        for obj in r.get("Contents", []):
            keys.append(obj["Key"])
        if r.get("IsTruncated") != "true":
            break
        # 腾讯云文档：截断时 NextMarker 可能缺失，此时应以本页最后一个
        # 对象的 Key 作为下一页起点。既无 NextMarker 又无本页内容时必须
        # 停下，否则会死循环。
        marker = r.get("NextMarker") or (keys[-1] if keys else "")
        if not marker:
            logger.warning(
                "cos_list_truncated_without_marker",
                extra={"prefix": prefix, "got": len(keys)},
            )
            break
    return keys


async def list_prefix(prefix: str) -> list[str]:
    """列出前缀下全部 key。单次 list_objects 上限 1000，故循环取到尽。"""
    client = get_client()
    try:
        return await asyncio.to_thread(_list_prefix_sync, client, prefix)
    except Exception as e:
        _log_cos_error("list_prefix", prefix, e)
        raise


async def delete_prefix(prefix: str) -> int:
    """删除前缀下全部对象。返回实际删除数量。批量删单次上限 1000，必须分批。

    ``Quiet='true'`` 下失败项仍会出现在响应的 ``Error`` 列表里，整个请求
    依然是 HTTP 200——绝不能无条件按 len(keys) 报成功，那是谎报。不抛
    异常（删不掉只留孤儿对象，符合本项目"宁可留孤儿对象"的一致性约定），
    但必须把部分失败如实记录、如实反映在返回值里。
    """
    keys = await list_prefix(prefix)
    if not keys:
        return 0
    client = get_client()

    def _do(chunk: list[str]) -> list[str]:
        """删除一批，返回本批中删除失败的 key。"""
        r = client.delete_objects(
            Bucket=bucket(),
            Delete={"Object": [{"Key": k} for k in chunk], "Quiet": "true"},
        )
        return [e.get("Key") for e in (r.get("Error") or [])]

    failed: list[str] = []
    for i in range(0, len(keys), 1000):
        try:
            failed.extend(await asyncio.to_thread(_do, keys[i:i + 1000]))
        except Exception as e:
            _log_cos_error("delete_prefix", prefix, e)
            raise

    deleted = len(keys) - len(failed)
    if failed:
        logger.error(
            "cos_delete_prefix_partial_failure",
            extra={
                "prefix": prefix, "deleted": deleted,
                "failed": len(failed), "failed_keys": failed[:20],
            },
        )
    logger.info("cos_delete_prefix", extra={"prefix": prefix, "count": deleted})
    return deleted


def signed_url(
    key: str,
    expires_sec: Optional[int] = None,
    filename: Optional[str] = None,
) -> str:
    """生成预签名 GET URL。**同步**——纯本地 HMAC 计算，不发网络请求。

    有效期取 min(配置 TTL, 凭证剩余有效期)：签名有效期不能超过临时密钥
    有效期，否则 URL 会在 TTL 内因凭证先过期而失效。

    filename 非空时附加 response-content-disposition，让 COS 直接返回附件
    下载头（用于成片下载）。
    """
    ttl = expires_sec if expires_sec is not None else settings.cos_signed_url_ttl_sec
    remaining = credentials_remaining_sec()
    if remaining is not None:
        ttl = min(ttl, remaining)

    params = {}
    if filename:
        params["response-content-disposition"] = f'attachment; filename="{filename}"'

    return get_client().get_presigned_url(
        Method="GET",
        Bucket=bucket(),
        Key=key,
        Expired=ttl,
        Params=params or None,
    )
