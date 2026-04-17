"""FastAPI application entry point."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import init_db

# Global Redis client
_redis_client: aioredis.Redis | None = None


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
from app.api import projects, pipeline, uploads, assets, stream, debug

app.include_router(projects.router, prefix="/api")
app.include_router(pipeline.router, prefix="/api")
app.include_router(uploads.router, prefix="/api")
app.include_router(assets.router, prefix="/api")
app.include_router(stream.router, prefix="/api")
app.include_router(debug.router, prefix="/api")

# Mount storage directory to serve generated media files (videos, frames)
from pathlib import Path as _Path
from fastapi.staticfiles import StaticFiles as _StaticFiles

_storage_dir = _Path(settings.storage_root)
_storage_dir.mkdir(parents=True, exist_ok=True)
app.mount("/api/media", _StaticFiles(directory=str(_storage_dir)), name="media")


@app.middleware("http")
async def no_cache_media(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/media/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response
