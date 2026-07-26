"""腾讯云 COS 客户端与凭证管理。

本模块是全项目唯一 import qcloud_cos 的地方；其余代码一律经由
object_store / workspace 访问 COS。

两个设计约束：
1. CosS3Client 必须单例 —— SDK 要求一个 region 只建一个实例并复用，
   否则进程会占用过多连接和线程。
2. 凭证缓存 —— to_media_url() 是同步函数（被 projects.py 的同步序列化器
   在列表推导中调用），而预签名需要凭证。static 模式凭证是常量无所谓；
   cvm_role 模式下取凭证是网络操作，不能在同步函数里做，因此由
   warm_credentials() 预热、后台协程周期刷新，签名时只读缓存。
"""

import asyncio
import logging
import time
from typing import Optional

import httpx
from qcloud_cos import CosConfig, CosS3Client

from app.config import settings

logger = logging.getLogger(__name__)

# CVM 实例角色的元数据服务地址
_METADATA_URL = (
    "http://metadata.tencentyun.com/latest/meta-data/cam/security-credentials/"
)

_client: Optional[CosS3Client] = None
_cached_cred: Optional[dict] = None
_cred_expires_at: Optional[float] = None  # unix 秒；None = 永不过期（static）
_refresh_task: Optional[asyncio.Task] = None


def bucket() -> str:
    """目标 bucket 名（含 AppId）。"""
    if not settings.cos_bucket:
        raise RuntimeError("未配置 cos_bucket")
    return settings.cos_bucket


def _fetch_cvm_role_credentials() -> dict:
    """从 CVM 实例元数据服务取 STS 临时密钥。同步阻塞调用。"""
    if not settings.cos_cvm_role:
        raise RuntimeError("cvm_role 模式需要 cos_cvm_role")
    r = httpx.get(f"{_METADATA_URL}{settings.cos_cvm_role}", timeout=5)
    r.raise_for_status()
    d = r.json()
    return {
        "secret_id": d["TmpSecretId"],
        "secret_key": d["TmpSecretKey"],
        "token": d["Token"],
        "expired_at": d.get("ExpiredTime"),
    }


def _build_config(cred: dict) -> CosConfig:
    kwargs = {
        "Region": settings.cos_region,
        "SecretId": cred["secret_id"],
        "SecretKey": cred["secret_key"],
        "Scheme": settings.cos_scheme,
    }
    if cred.get("token"):
        kwargs["Token"] = cred["token"]
    if settings.cos_domain:
        kwargs["Domain"] = settings.cos_domain
    return CosConfig(**kwargs)


def get_client() -> CosS3Client:
    """CosS3Client 单例。"""
    global _client
    if _client is None:
        _client = CosS3Client(_build_config(get_cached_credentials()))
        logger.info(
            "cos_client_created",
            extra={"region": settings.cos_region, "bucket": settings.cos_bucket,
                   "auth_mode": settings.cos_auth_mode},
        )
    return _client


async def warm_credentials() -> None:
    """预热凭证缓存。FastAPI lifespan 与 worker 启动时调用。

    cvm_role 模式下临时密钥会变，凭证是构造 CosConfig 时传入的，
    因此刷新凭证必须一并重建 client —— 这是与永久密钥模式的关键差异。
    """
    global _cached_cred, _cred_expires_at, _client

    if settings.cos_auth_mode == "static":
        if not (settings.cos_secret_id and settings.cos_secret_key):
            raise RuntimeError("static 模式需要 cos_secret_id / cos_secret_key")
        _cached_cred = {
            "secret_id": settings.cos_secret_id,
            "secret_key": settings.cos_secret_key,
            "token": None,
        }
        _cred_expires_at = None
        return

    cred = await asyncio.to_thread(_fetch_cvm_role_credentials)
    _cached_cred = cred
    expired_at = cred.get("expired_at")
    _cred_expires_at = float(expired_at) if expired_at else time.time() + 3600
    _client = None  # 强制下次 get_client() 用新凭证重建
    logger.info("cos_credentials_warmed", extra={"auth_mode": settings.cos_auth_mode})


async def _refresh_loop() -> None:
    """按凭证剩余有效期的 50% 周期刷新，保证同步签名路径永远读到有效凭证。"""
    while True:
        try:
            remaining = credentials_remaining_sec() or 3600
            await asyncio.sleep(max(60, remaining // 2))
            await warm_credentials()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("cos_credentials_refresh_failed")
            await asyncio.sleep(60)


async def start_credential_refresh() -> None:
    """启动后台刷新协程（仅 cvm_role 模式需要）。"""
    global _refresh_task
    if settings.cos_auth_mode != "cvm_role":
        return
    if _refresh_task is None or _refresh_task.done():
        _refresh_task = asyncio.create_task(_refresh_loop())


def get_cached_credentials() -> dict:
    """同步读取缓存凭证。缓存为空时抛错，绝不阻塞去取。"""
    if _cached_cred is None:
        raise RuntimeError(
            "COS 凭证缓存为空；应在应用启动时调用 warm_credentials()"
        )
    return _cached_cred


def credentials_remaining_sec() -> Optional[int]:
    """缓存凭证剩余有效秒数。static 模式返回 None（永不过期）。"""
    if _cred_expires_at is None:
        return None
    return max(0, int(_cred_expires_at - time.time()))


async def close_client() -> None:
    """停止后台刷新并释放单例。幂等。"""
    global _client, _refresh_task
    if _refresh_task is not None:
        _refresh_task.cancel()
        try:
            await _refresh_task
        except (asyncio.CancelledError, Exception):
            pass
        _refresh_task = None
    _client = None
