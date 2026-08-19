"""Worker startup() 必须守住 Alembic head 检查——同 backend 的 init_db()。

背景：Phase 0 把表结构管理权交给了 Alembic，`app/db.py` 的 `init_db()`
不再建表，改为调用 `assert_migrations_current(engine)` 做启动守门——库没
升到 Alembic head 就抛 RuntimeError 拒绝启动。但当时只有 `app/main.py` 的
FastAPI lifespan 接了这道线，两个 arq worker（arq_worker.py /
vc_arq_worker.py）的 `startup()` 建完引擎后没有调用它。

这个缺口在迁移前不存在——`init_db()` 那时会 `create_all` 建表，worker 即使
先于 backend 启动也没事；现在没有任何东西建表了，而本项目部署惯例里存在
分开重启 backend 与 worker 的路径（见 CLAUDE.md 的
`podman restart video-maker-backend-dev video-maker-worker-dev`），worker
完全可能在库版本滞后时启动。届时它不会干净失败，而是在处理某个正在计费的
任务中途以 UndefinedColumn / UndefinedTable 这类底层 SQL 错误炸掉——代价
远高于启动时明确失败。本测试钉住修复：两个 worker 的 `startup()` 现在会
在库未到 head 时抛错拒绝启动。

`test_init_db_head_check.py` 测的是 `assert_migrations_current` 本身；这里
测的是 worker `startup()` 有没有接上这道线。`startup()` 里还会 warm COS
凭证、连 redis——这两块与本次改动无关，用 monkeypatch 短路掉，只验证 DB
守门这一条路径是否生效、是否在 ctx 被任何任务使用之前就挡住了。
"""

import asyncio
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.config import settings
from worker import arq_worker, vc_arq_worker

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _sync_test_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL_SYNC",
        "postgresql://videomaker:devpassword@localhost:5433/videomaker_test",
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


def _point_settings_at_test_db(monkeypatch):
    """让 worker 的 resolved_database_url 指向真实的 videomaker_test 库，
    而不是默认的 sqlite 回退值——同 test_worker_db_engine.py 的做法。
    """
    monkeypatch.setattr(settings, "postgres_host", "localhost")
    monkeypatch.setattr(settings, "postgres_port", 5433)
    monkeypatch.setattr(settings, "postgres_db", "videomaker_test")
    monkeypatch.setattr(settings, "postgres_user", "videomaker")
    monkeypatch.setattr(settings, "postgres_password", "devpassword")
    monkeypatch.setattr(settings, "postgres_password_file", "")


def _stub_out_cos_and_redis(monkeypatch, worker_module):
    """startup() 里 warm COS 凭证 + 连 redis 与本次改动（DB 守门）无关，
    短路掉以避免测试依赖真实 COS 凭证/redis 网络往返。
    """

    async def _noop_cos(*_args, **_kwargs):
        return None

    monkeypatch.setattr(worker_module.cos_client, "warm_credentials", _noop_cos)
    monkeypatch.setattr(worker_module.cos_client, "start_credential_refresh", _noop_cos)

    class _FakeRedis:
        async def aclose(self):
            return None

    async def _fake_from_url(*_args, **_kwargs):
        return _FakeRedis()

    monkeypatch.setattr(worker_module.aioredis, "from_url", _fake_from_url)


async def test_arq_worker_startup_rejects_db_not_at_head(monkeypatch, empty_db):
    _point_settings_at_test_db(monkeypatch)
    _stub_out_cos_and_redis(monkeypatch, arq_worker)

    ctx = {}
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        await arq_worker.startup(ctx)

    # 守门必须先于任务处理生效：抛错时引擎还没被交给 ctx，worker 不会带着
    # 一个指向过期 schema 的引擎去接任务。
    assert "engine" not in ctx


async def test_vc_arq_worker_startup_rejects_db_not_at_head(monkeypatch, empty_db):
    _point_settings_at_test_db(monkeypatch)
    _stub_out_cos_and_redis(monkeypatch, vc_arq_worker)

    ctx = {}
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        await vc_arq_worker.startup(ctx)

    assert "engine" not in ctx


async def test_arq_worker_startup_passes_when_db_at_head(
    monkeypatch, empty_db, alembic_config
):
    await asyncio.to_thread(command.upgrade, alembic_config, "head")
    _point_settings_at_test_db(monkeypatch)
    _stub_out_cos_and_redis(monkeypatch, arq_worker)

    ctx = {}
    try:
        await arq_worker.startup(ctx)  # 不抛异常即通过
        assert "engine" in ctx
    finally:
        if "engine" in ctx:
            await ctx["engine"].dispose()


async def test_vc_arq_worker_startup_passes_when_db_at_head(
    monkeypatch, empty_db, alembic_config
):
    await asyncio.to_thread(command.upgrade, alembic_config, "head")
    _point_settings_at_test_db(monkeypatch)
    _stub_out_cos_and_redis(monkeypatch, vc_arq_worker)

    ctx = {}
    try:
        await vc_arq_worker.startup(ctx)  # 不抛异常即通过
        assert "engine" in ctx
    finally:
        if "engine" in ctx:
            await ctx["engine"].dispose()
