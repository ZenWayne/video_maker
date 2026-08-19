"""arq worker configuration and settings."""

import logging
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import redis.asyncio as aioredis

from app.config import settings
from app.db import assert_migrations_current, build_pool_kwargs
from app.services import cos_client
from worker.tasks import (
    run_screenwriter,
    run_shot_pipeline,
    run_merger,
    run_character_calibrate,
    run_character_calibrate_batch,
    run_image_candidate,
    run_content_analysis,
)

# Configure app/worker loggers so INFO+ messages reach stderr
_fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(filename)s:%(lineno)d: %(message)s", datefmt="%H:%M:%S")
_handler = logging.StreamHandler()
_handler.setFormatter(_fmt)
for _name in ("worker", "app"):
    _logger = logging.getLogger(_name)
    _logger.setLevel(logging.INFO)
    _logger.addHandler(_handler)


def _build_db_engine():
    """Build the worker's DB engine — split out from ``startup`` purely so it
    can be unit-tested without also standing up Redis/COS. Must use
    ``resolved_database_url`` (the single source of truth introduced in Phase
    0 Task 1), not the raw ``database_url`` fallback: Task 5 removed
    DATABASE_URL from compose, so ``database_url`` now silently falls back to
    sqlite — pointing the worker at a local, table-less sqlite file while the
    backend talks to PostgreSQL.
    """
    database_url = settings.resolved_database_url
    return create_async_engine(
        database_url,
        echo=False,
        future=True,
        **build_pool_kwargs(database_url),
    )


async def startup(ctx: dict) -> None:
    """Startup hook - create Redis and DB connections."""
    # COS credentials must be warmed before any job runs: to_media_url() (used
    # by observability spans / event payloads) is a sync function that only
    # reads the cache — see app.services.cos_client for why. Import from
    # cos_client directly (zero app.api dependency) — never from app.main,
    # which would drag the whole FastAPI app (and its routers) into a
    # standalone worker process.
    await cos_client.warm_credentials()
    await cos_client.start_credential_refresh()

    # Redis connection
    ctx["redis"] = await aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )

    # Database engine and session factory
    engine = _build_db_engine()
    # 库没升到 Alembic head 就拒绝启动——迁移到 Alembic 之后表结构不再由
    # create_all 兜底，backend 的 init_db() 已经这样守门（见 app/main.py
    # lifespan），worker 是独立进程、必须自己也守一道：否则库版本滞后时
    # worker 不会干净失败，而是在处理某个正在计费的任务中途以
    # UndefinedColumn/UndefinedTable 这类底层 SQL 错误炸掉。
    await assert_migrations_current(engine)
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

    await cos_client.close_client()


class WorkerSettings:
    """arq worker settings."""

    # Redis connection
    redis_settings = RedisSettings.from_dsn(settings.redis_url)

    # Functions that can be enqueued
    functions = [
        run_screenwriter,
        run_shot_pipeline,
        run_merger,
        run_character_calibrate,
        run_character_calibrate_batch,
        run_image_candidate,
        run_content_analysis,
    ]

    # Worker settings
    max_jobs = settings.worker_pool_size
    job_timeout = 1800  # 30 minutes

    # Lifecycle hooks
    on_startup = startup
    on_shutdown = shutdown

    # Logging
    log_level = "info"
