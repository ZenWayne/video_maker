"""应用级鉴权：口令哈希、会话存储、调用方身份解析。

三条身份通道，优先级从高到低：

1. ``Authorization: Bearer <machine_token>`` —— 机器凭据（FR-5）。MCP 这类
   非浏览器调用方没有浏览器、不存 cookie、无法交互登录，必须有独立通道。
2. 会话 cookie —— 浏览器（含 SSE，EventSource 不支持自定义请求头，所以凭据
   只能放 cookie）。
3. 无凭据 —— 只有 ``AUTH_ENFORCED=false`` 时才放行，身份为 ``None``。

会话存 redis（7 天滑动过期），账号存数据库；**数据库里只有口令哈希，没有任何
可直接用于登录的 secret**。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets as pysecrets
from dataclasses import dataclass
from typing import Optional

import bcrypt
import redis.asyncio as aioredis
from sqlalchemy import select

from app.config import settings

logger = logging.getLogger(__name__)

SESSION_KEY_PREFIX = "session:"
REGISTER_RATE_KEY_PREFIX = "reg_rate:"

# 本进程自建的 redis 连接。正常运行时 app.main 已经建好一个，直接复用；
# 独立进程（脚本 / 测试）里才会走到懒建这条路。
_own_redis: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    """会话/限流用的 redis 客户端。

    优先复用 app.main 的全局客户端——它由 lifespan 管理，连接数可控。
    **必须在调用时才读 app.main._redis_client**：模块导入期它还是 None。
    """
    global _own_redis
    from app import main as app_main

    if app_main._redis_client is not None:
        return app_main._redis_client
    if _own_redis is None:
        _own_redis = await aioredis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )
    return _own_redis


async def close_own_redis() -> None:
    global _own_redis
    if _own_redis is not None:
        await _own_redis.aclose()
        _own_redis = None


# ── 口令哈希 ────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """bcrypt 哈希（明文口令**绝不入库**）。

    先做一遍 sha256 再 base64，是因为 bcrypt 只吃前 72 字节：直接喂长口令，
    要么被静默截断（长口令反而变弱），要么在 bcrypt>=4 里直接抛异常。预哈希
    把任意长度压成固定 44 字节，是 passlib ``bcrypt_sha256`` 的同款做法。
    """
    digest = base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())
    return bcrypt.hashpw(digest, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    digest = base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())
    try:
        return bcrypt.checkpw(digest, password_hash.encode("ascii"))
    except (ValueError, TypeError):
        # 哈希串损坏/格式不认识 —— 当作校验失败，不要把异常抛到登录端点，
        # 否则 500 会把「这个用户名存在」泄漏出去。
        return False


# ── 身份 ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Principal:
    """一次请求的调用方身份。

    身份**恒有账号**（``user_id`` 非空）。曾经存在一种「服务主体」——机器令牌
    有效但没绑账号时给一个无账号身份——已经删掉：那意味着配置漏填换来的是最大
    权限（绕过归属过滤、不扣点数），与 FR-3「默认拒绝」的原则正好相反。现在
    没绑账号的机器令牌直接判未鉴权。
    """

    username: str
    user_id: Optional[str] = None
    is_admin: bool = False
    is_machine: bool = False
    session_token: Optional[str] = None
    # 访客身份：只读。归属过滤和点数照常生效，另外由中间件挡掉所有非 GET
    # 请求——光靠 0 点余额挡不住删项目、改文案、裁剪这类不花钱的写操作。
    is_guest: bool = False

    @property
    def is_billable(self) -> bool:
        """是否有账号可以扣点数/校验归属。

        按现在的构造路径恒为 True（会话与机器令牌都必然带账号）。保留它是**兜底**：
        万一将来又冒出一种无账号身份，归属过滤和扣费的调用点会自动把它当成
        「不可计费」而不是默默放行。
        """
        return self.user_id is not None


def _session_key(token: str) -> str:
    return f"{SESSION_KEY_PREFIX}{token}"


async def create_session(user) -> str:
    """签发会话并写 redis，返回 token。

    ⚠️ 签发**不受 AUTH_ENFORCED 控制**：开关只管「未认证是否放行」。否则
    P1/P2 阶段（开关为 false）根本没法验证登录链路。
    """
    token = pysecrets.token_urlsafe(32)
    redis = await get_redis()
    await redis.set(
        _session_key(token),
        json.dumps({
            "user_id": user.id,
            "username": user.username,
            "is_admin": bool(user.is_admin),
        }),
        ex=settings.session_ttl_days * 86400,
    )
    return token


async def destroy_session(token: str) -> None:
    redis = await get_redis()
    await redis.delete(_session_key(token))


async def load_session(token: str) -> Optional[Principal]:
    """校验会话并**滑动续期**（7 天从最后一次请求起算）。"""
    redis = await get_redis()
    key = _session_key(token)
    raw = await redis.get(key)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        await redis.delete(key)
        return None
    await redis.expire(key, settings.session_ttl_days * 86400)
    return Principal(
        username=data["username"],
        user_id=data.get("user_id"),
        is_admin=bool(data.get("is_admin")),
        session_token=token,
    )


async def _machine_principal() -> Optional[Principal]:
    """机器令牌对应的身份。

    绑定了 ``MACHINE_TOKEN_USER`` 就完全等同该用户：同样按 owner 过滤、同样扣
    点数、同样继承那个账号的 ``is_admin``。

    **没绑定（或绑的账号不存在/已停用）→ 返回 None，视同未鉴权。** 令牌本身只
    回答了「是不是我们自己人」，回答不了「能看哪些项目」「扣谁的点数」——放它
    进来就只能是无边界的全权限，等于配置漏填换来最大权限。这与 FR-3 的默认拒绝
    是同一条原则：配置不全时应当进不去，而不是裸奔。

    分期上线下的效果：开关关着时它落回匿名（与鉴权上线前一致，不打断 MCP），
    开关一打开就 401——**恰好在强制校验生效的那一刻响亮地失败**。
    """
    username = (settings.machine_token_user or "").strip()
    if not username:
        logger.warning(
            "机器令牌有效但 MACHINE_TOKEN_USER 未配置 —— 按未鉴权处理。"
            "绑定一个账号后 MCP 才能在强制校验下工作。"
        )
        return None

    from app import db as db_module
    from app.models.project import User

    async with db_module.AsyncSession() as session:
        user = (await session.execute(
            select(User).where(User.username == username)
        )).scalar_one_or_none()
    if user is None or not user.is_active:
        logger.warning(
            "MACHINE_TOKEN_USER=%r 不存在或已停用 —— 机器令牌按未鉴权处理", username,
        )
        return None

    return Principal(
        username=user.username,
        user_id=user.id,
        is_admin=bool(user.is_admin),
        is_machine=True,
    )


async def guest_principal() -> Optional[Principal]:
    """未登录访客对应的身份；未配置 GUEST_USERNAME 时返回 None。

    访客是一个**真实账号**，不是特例分支：因此它自动继承已有的两道约束——
    owner 过滤让它只看得见自己名下的演示数据，0 点余额让它碰不了任何计费操作。
    只读则由中间件另外强制（见 is_guest）。
    """
    username = (settings.guest_username or "").strip()
    if not username:
        return None

    from app import db as db_module
    from app.models.project import User

    async with db_module.AsyncSession() as session:
        user = (await session.execute(
            select(User).where(User.username == username)
        )).scalar_one_or_none()
    if user is None or not user.is_active:
        logger.warning("GUEST_USERNAME=%r 不存在或已停用 —— 访客模式未生效", username)
        return None

    return Principal(
        username=user.username,
        user_id=user.id,
        is_admin=False,   # 访客永远不继承管理员位，哪怕账号被误设成管理员
        is_guest=True,
    )


def _bearer_token(headers: dict[bytes, bytes]) -> Optional[str]:
    raw = headers.get(b"authorization")
    if not raw:
        return None
    value = raw.decode("latin-1")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _cookie_value(headers: dict[bytes, bytes], name: str) -> Optional[str]:
    raw = headers.get(b"cookie")
    if not raw:
        return None
    for part in raw.decode("latin-1").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v or None
    return None


async def resolve_principal(headers: dict[bytes, bytes]) -> Optional[Principal]:
    """从请求头解析身份；无凭据或凭据无效返回 None。"""
    token = _bearer_token(headers)
    if token is not None:
        configured = (settings.machine_token or "").strip()
        # compare_digest：令牌比对必须定长，避免按字节提前返回泄漏前缀。
        if configured and pysecrets.compare_digest(token, configured):
            return await _machine_principal()
        # Bearer 值也可能是会话 token（非浏览器客户端拿 cookie 值直接用）。
        session_principal = await load_session(token)
        if session_principal is not None:
            return session_principal
        return None

    cookie = _cookie_value(headers, settings.session_cookie_name)
    if cookie:
        return await load_session(cookie)
    return None


# ── 注册限流（FR-10） ───────────────────────────────────────────────────────

def client_ip(headers: dict[bytes, bytes], fallback: Optional[str]) -> str:
    """取真实客户端 IP。

    **必须优先用 CF-Connecting-IP**：线上前面压着 Cloudflare + cn-ingress，
    socket 地址拿到的是它们的地址，那样全站共用一个计数器，第一个注册的人就
    把所有人挡在门外。X-Forwarded-For 取**最左**段（Cloudflare 写入的原始
    客户端），仅在没有 CF 头时兜底。
    """
    cf = headers.get(b"cf-connecting-ip")
    if cf:
        value = cf.decode("latin-1").strip()
        if value:
            return value
    xff = headers.get(b"x-forwarded-for")
    if xff:
        first = xff.decode("latin-1").split(",")[0].strip()
        if first:
            return first
    return fallback or "unknown"


async def check_register_rate_limit(ip: str) -> bool:
    """返回 True 表示放行；False 表示超限（调用方回 429）。

    计数器只在**首次**递增时设 TTL —— 用 expire(nx) 而不是无条件 expire，
    否则每次尝试都把窗口往后推，限流窗口永远不会结束。
    """
    if settings.register_rate_limit <= 0:
        return True
    redis = await get_redis()
    key = f"{REGISTER_RATE_KEY_PREFIX}{ip}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, settings.register_rate_limit_window_sec)
    return count <= settings.register_rate_limit
