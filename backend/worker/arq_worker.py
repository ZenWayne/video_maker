"""arq worker configuration and settings."""

import logging
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import redis.asyncio as aioredis

from app.config import settings
from worker.tasks import (
    run_screenwriter,
    run_shot_pipeline,
    run_merger,
    run_voice_convert,
    run_voice_convert_batch,
)

# Configure app/worker loggers so INFO+ messages reach stderr
_fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(filename)s:%(lineno)d: %(message)s", datefmt="%H:%M:%S")
_handler = logging.StreamHandler()
_handler.setFormatter(_fmt)
for _name in ("worker", "app"):
    _logger = logging.getLogger(_name)
    _logger.setLevel(logging.INFO)
    _logger.addHandler(_handler)


async def startup(ctx: dict) -> None:
    """Startup hook - create Redis and DB connections."""
    # Redis connection
    ctx["redis"] = await aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )

    # Database engine and session factory
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        future=True,
    )
    ctx["engine"] = engine
    ctx["session_factory"] = async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
    )


async def shutdown(ctx: dict) -> None:
    """Shutdown hook - cleanup connections."""
    # Close Redis
    if "redis" in ctx:
        await ctx["redis"].aclose()

    # Dispose database engine
    if "engine" in ctx:
        await ctx["engine"].dispose()


class WorkerSettings:
    """arq worker settings."""

    # Redis connection
    redis_settings = RedisSettings.from_dsn(settings.redis_url)

    # Functions that can be enqueued
    functions = [
        run_screenwriter,
        run_shot_pipeline,
        run_merger,
        run_voice_convert,
        run_voice_convert_batch,
    ]

    # Worker settings
    max_jobs = settings.worker_pool_size
    job_timeout = 1800  # 30 minutes

    # Lifecycle hooks
    on_startup = startup
    on_shutdown = shutdown

    # Logging
    log_level = "info"
