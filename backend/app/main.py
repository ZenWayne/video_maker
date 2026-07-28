"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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
    await cos_client.close_client()


# Create FastAPI app
app = FastAPI(
    title="Video Maker API",
    description="API for AI-powered video generation with Gemini and Veo 3",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
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
                     voice, image_candidates, content_analysis)

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
