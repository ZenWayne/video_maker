"""init_db 不再建表，改为守门：库没升到 head 就拒绝启动。

这条守门很关键——迁移到 Alembic 之后，「表不存在」不再会被 create_all
悄悄兜住。宁可启动时明确报错，也不要跑到第一个查询才炸。
"""

import asyncio
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import assert_migrations_current

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _sync_test_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL_SYNC",
        "postgresql://videomaker:devpassword@localhost:5433/videomaker_test",
    )


def _async_test_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://videomaker:devpassword@localhost:5433/videomaker_test",
    )


@pytest.fixture
def alembic_config():
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", _sync_test_url())
    return cfg


@pytest.fixture
def empty_db():
    engine = create_engine(_sync_test_url())
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield
    engine.dispose()


async def test_raises_when_db_not_migrated(empty_db):
    engine = create_async_engine(_async_test_url())
    try:
        with pytest.raises(RuntimeError, match="alembic upgrade head"):
            await assert_migrations_current(engine)
    finally:
        await engine.dispose()


async def test_passes_when_db_at_head(empty_db, alembic_config):
    # command.upgrade drives Alembic's async env.py, which internally calls
    # asyncio.run() (see alembic/env.py:run_migrations_online). This test
    # function already runs inside pytest-asyncio's event loop, so a bare
    # `command.upgrade(...)` call here would hit "asyncio.run() cannot be
    # called from a running event loop". Running it in a separate thread
    # gives it its own fresh loop and sidesteps the nesting conflict.
    await asyncio.to_thread(command.upgrade, alembic_config, "head")
    engine = create_async_engine(_async_test_url())
    try:
        await assert_migrations_current(engine)  # 不抛异常即通过
    finally:
        await engine.dispose()
