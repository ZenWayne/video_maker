"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import MutableHeaders
from fastapi.responses import JSONResponse

from app.auth_middleware import AuthMiddleware
from app.config import settings
from app.db import init_db
from app.services import cos_client

logger = logging.getLogger(__name__)

# Global Redis client
_redis_client: aioredis.Redis | None = None


def check_video_provider() -> None:
    """Log the active video provider, warning loudly if it is not ``vertex``.

    Project policy mandates Vertex AI (Veo via Google). The committed
    ``deploy/config.yml`` defaults ``VIDEO_PROVIDER`` to ``vertex``, but the
    gitignored ``deploy/config.env`` (loaded by compose as ``env_file``) can
    silently override it back to ``kie``. This makes such a revert visible in
    the backend logs at startup instead of failing silently.
    """
    if settings.video_provider != "vertex":
        logger.warning(
            "VIDEO_PROVIDER=%r is NOT 'vertex' — project policy requires Vertex AI. "
            "deploy/config.env likely overrides the committed config.yml default; "
            "run `make config` to resync it back to vertex.",
            settings.video_provider,
        )
    else:
        logger.info("Video provider: vertex (Veo via Vertex AI)")


def check_auth_config() -> None:
    """把鉴权/计费开关的实际状态打到日志里。

    ``AUTH_ENFORCED`` 决定「未认证是否放行」，是唯一的回滚开关；它写在
    gitignore 的 config.env 里，容易与预期不符，所以每次启动都明说一次。
    """
    if settings.auth_enforced:
        logger.info("AUTH_ENFORCED=true — 未认证请求一律 401，项目按 owner_id 严格隔离")
    else:
        logger.warning(
            "AUTH_ENFORCED=false — 未认证请求照常放行（与鉴权上线前行为一致）。"
            "会话/注册/点数接口可用，但不构成任何访问控制。"
        )
    if settings.machine_token and not settings.machine_token_user:
        logger.warning(
            "机器令牌已配置但未绑定账号（MACHINE_TOKEN_USER 为空）：该令牌一律"
            "按未鉴权处理。开关关着时 MCP 落回匿名照常可用；一旦 AUTH_ENFORCED=true "
            "就会 401。上线强制校验前必须绑定一个账号。"
        )


async def get_redis() -> aioredis.Redis:
    """Get Redis client dependency."""
    if _redis_client is None:
        raise RuntimeError("Redis not initialized")
    return _redis_client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan - startup and shutdown."""
    global _redis_client

    # Startup
    # Surface the active video provider (loud warning if not vertex)
    check_video_provider()
    check_auth_config()

    # Credentials must be warmed before serving: to_media_url() is a *sync*
    # function (called from ~50 sync serializers) that only reads the cache —
    # it never blocks to fetch. Without this, the first request touching any
    # media field raises "COS 凭证缓存为空".
    await cos_client.warm_credentials()
    await cos_client.start_credential_refresh()

    # Initialize database tables
    await init_db()

    # Initialize Redis connection
    _redis_client = await aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )

    yield

    # Shutdown
    if _redis_client:
        await _redis_client.aclose()
        _redis_client = None
    # 会话服务在没有全局客户端时会自建一条连接（脚本/测试路径），一并关掉。
    from app.services.auth import close_own_redis
    await close_own_redis()
    await cos_client.close_client()


# Create FastAPI app
app = FastAPI(
    title="Video Maker API",
    description="API for AI-powered video generation with Gemini and Veo 3",
    version="0.1.0",
    lifespan=lifespan,
)

class NoStoreAPIMiddleware:
    """Mark every /api/ response as uncacheable.

    The API sent no cache headers at all, which lets any cache in front of it
    decide for itself. That is not hypothetical: the Vercel-hosted frontend
    proxies /api/* and its edge cache was serving `x-vercel-cache: HIT` with
    `age: 18111` — i.e. ~5h-stale analysis lists.

    It is also a correctness/privacy issue, not just staleness: callers are
    identified by the `X-User-Name` header (see api/projects.py `_require_user`)
    and caches do not key on that header, so one user's cached response can be
    served to another. `no-store` is the blunt, correct answer for an API whose
    responses are all per-user or fast-changing; nothing here is worth caching.

    Applied at the app layer on purpose rather than in a proxy config, so it
    holds no matter what sits in front (Vercel, Cloudflare, nginx, none).

    Written as raw ASGI middleware rather than `@app.middleware("http")`:
    the latter is `BaseHTTPMiddleware`, which wraps the response body and has
    a long history of breaking streaming responses. Two endpoints here are SSE
    (`/api/projects/{id}/stream`, `/api/analyses/{id}/stream`) and keeping them
    unbuffered took real effort — this version only rewrites the response
    headers and never touches the body stream.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return

        async def send_with_no_store(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["cache-control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_no_store)


# ── 中间件顺序 ──────────────────────────────────────────────────────────────
# add_middleware 是**头插**：最后添加的包在最外层。所以下面的顺序意味着
#   CORS → NoStore → Auth → 路由
# CORS 必须在最外层：AuthMiddleware 直接构造 401 响应返回，如果 CORS 在它
# 里面，这个 401 就不带 Access-Control-Allow-Origin，浏览器只会报一个跨域
# 错误，前端连「我未登录」都读不到，无从触发跳登录页。
# NoStore 在 Auth 外面：401/404 这类拒绝响应同样不该被任何中间缓存留下。
app.add_middleware(AuthMiddleware)
app.add_middleware(NoStoreAPIMiddleware)
app.add_middleware(
    CORSMiddleware,
    # allow_credentials=True 时规范禁止用 "*"，所以来源必须精确列举。
    # 已知坑：逗号后带空格会静默失配，这里统一 strip 掉。
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    """Handle generic exceptions."""
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": str(exc)}},
    )


# Health check endpoint
@app.get("/health", response_model=dict)
async def health_check():
    """Health check endpoint."""
    redis_status = "ok"

    try:
        if _redis_client:
            await _redis_client.ping()
        else:
            redis_status = "not_initialized"
    except Exception as e:
        redis_status = f"error: {e}"

    return {
        "status": "ok",
        "redis": redis_status,
        "db": "ok",  # If we got here, DB is working
    }


# Import and include routers
from app.api import (projects, pipeline, uploads, assets, stream, debug,
                     voice, image_candidates, content_analysis, auth, admin)

app.include_router(auth.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
app.include_router(projects.router, prefix="/api")
app.include_router(pipeline.router, prefix="/api")
app.include_router(voice.router, prefix="/api")
app.include_router(uploads.router, prefix="/api")
app.include_router(assets.router, prefix="/api")
app.include_router(stream.router, prefix="/api")
app.include_router(debug.router, prefix="/api")
app.include_router(image_candidates.router, prefix="/api")
app.include_router(content_analysis.router, prefix="/api")

# 注:/api/media 静态挂载 + no-cache 中间件已删除(Task 12)。媒体一律走
# to_media_url() 产出的 COS 预签名 URL——留着本地静态挂载等于留了一条绕过
# 签名的后门,且 key 里内建的 ts_uuid 已经保证了唯一性,不再需要 no-cache
# 中间件防止浏览器缓存过期文件。
