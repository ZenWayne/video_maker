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
            # 高级接口按大小自动选简单/分块上传，分块自带断点续传
            return client.upload_file(
                Bucket=bucket(), Key=key, LocalFilePath=str(local_path),
                PartSize=10, MAXThread=10, EnableMD5=False,
            )
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


async def list_prefix(prefix: str) -> list[str]:
    """列出前缀下全部 key。单次 list_objects 上限 1000，故循环取到尽。"""
    client = get_client()

    def _do() -> list[str]:
        keys: list[str] = []
        marker = ""
        while True:
            r = client.list_objects(Bucket=bucket(), Prefix=prefix, Marker=marker)
            for obj in r.get("Contents", []):
                keys.append(obj["Key"])
            if r.get("IsTruncated") != "true":
                break
            marker = r["NextMarker"]
        return keys

    try:
        return await asyncio.to_thread(_do)
    except Exception as e:
        _log_cos_error("list_prefix", prefix, e)
        raise


async def delete_prefix(prefix: str) -> int:
    """删除前缀下全部对象。返回删除数量。批量删单次上限 1000，必须分批。"""
    keys = await list_prefix(prefix)
    if not keys:
        return 0
    client = get_client()

    def _do(chunk: list[str]):
        return client.delete_objects(
            Bucket=bucket(),
            Delete={"Object": [{"Key": k} for k in chunk], "Quiet": "true"},
        )

    for i in range(0, len(keys), 1000):
        try:
            await asyncio.to_thread(_do, keys[i:i + 1000])
        except Exception as e:
            _log_cos_error("delete_prefix", prefix, e)
            raise
    logger.info("cos_delete_prefix", extra={"prefix": prefix, "count": len(keys)})
    return len(keys)


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
