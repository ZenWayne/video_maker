"""arq worker settings for voice conversion tasks only.

Runs in the vc-worker container (has vc2 + ONNX models installed).
The main worker handles all other tasks.
"""

import logging
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import redis.asyncio as aioredis

from app.config import settings
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


async def startup(ctx: dict) -> None:
    ctx["redis"] = await aioredis.from_url(
        settings.redis_url, encoding="utf-8", decode_responses=True
    )
    engine = create_async_engine(settings.database_url, echo=False, future=True)
    ctx["engine"] = engine
    ctx["session_factory"] = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )


async def shutdown(ctx: dict) -> None:
    if "redis" in ctx:
        await ctx["redis"].aclose()
    if "engine" in ctx:
        await ctx["engine"].dispose()


class VcWorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    queue_name = "arq:vc"          # separate queue — won't steal main worker jobs
    functions = [run_voice_convert, run_voice_convert_batch]
    max_jobs = 2                   # VC is CPU-heavy; keep concurrency low
    job_timeout = 1800
    on_startup = startup
    on_shutdown = shutdown
    log_level = "info"
