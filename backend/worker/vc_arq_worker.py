"""arq worker settings for voice conversion tasks only.

Runs in the vc-worker container (has vc2 + ONNX models installed).
The main worker handles all other tasks.
"""

import logging
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import redis.asyncio as aioredis

from app.config import settings
from app.db import assert_migrations_current, build_pool_kwargs
from app.services import cos_client
from worker.tasks import run_voice_convert, run_voice_convert_batch

_fmt = logging.Formatter(
    "%(asctime)s [%(name)s] %(levelname)s %(filename)s:%(lineno)d: %(message)s",
    datefmt="%H:%M:%S",
)
_handler = logging.StreamHandler()
_handler.setFormatter(_fmt)
for _name in ("worker", "app"):
    _logger = logging.getLogger(_name)
    _logger.setLevel(logging.INFO)
    _logger.addHandler(_handler)


def _build_db_engine():
    """Build the worker's DB engine — split out from ``startup`` purely so it
    can be unit-tested without also standing up Redis/COS. See
    ``arq_worker._build_db_engine``: must use ``resolved_database_url``, not
    the raw ``database_url`` fallback (which silently points at sqlite now
    that Task 5 removed DATABASE_URL from compose).
    """
    database_url = settings.resolved_database_url
    return create_async_engine(
        database_url,
        echo=False,
        future=True,
        **build_pool_kwargs(database_url),
    )


async def startup(ctx: dict) -> None:
    # See arq_worker.startup: cos_client is a zero-app.api-dependency module —
    # never import warm_credentials via app.main from a worker process.
    await cos_client.warm_credentials()
    await cos_client.start_credential_refresh()

    ctx["redis"] = await aioredis.from_url(
        settings.redis_url, encoding="utf-8", decode_responses=True
    )
    engine = _build_db_engine()
    # 见 arq_worker.startup 的同一处注释：库没升到 Alembic head 就拒绝启动，
    # 避免 vc-worker 在处理一个正在计费的语音转换任务中途才炸出底层 SQL 错误。
    await assert_migrations_current(engine)
    ctx["engine"] = engine
    ctx["session_factory"] = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )


async def shutdown(ctx: dict) -> None:
    if "redis" in ctx:
        await ctx["redis"].aclose()
    if "engine" in ctx:
        await ctx["engine"].dispose()
    await cos_client.close_client()


class VcWorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    queue_name = "arq:vc"          # separate queue — won't steal main worker jobs
    functions = [run_voice_convert, run_voice_convert_batch]
    max_jobs = 2                   # VC is CPU-heavy; keep concurrency low
    job_timeout = 1800
    on_startup = startup
    on_shutdown = shutdown
    log_level = "info"
