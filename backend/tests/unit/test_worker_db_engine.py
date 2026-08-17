"""Worker DB 引擎必须用 resolved_database_url，而不是 database_url 回退值。

背景（Critical 缺陷）：Phase 0 Task 1 引入 settings.resolved_database_url 作为
「唯一真相」——host/port/user/password/db 分量一旦设了 postgres_host 就优先拼出
PostgreSQL URL；database_url 只是本地开发/回滚用的 sqlite 回退值。但两个 arq
worker（arq_worker.py / vc_arq_worker.py）建引擎时最初直接读了
settings.database_url。Task 5 又把 DATABASE_URL 环境变量从 compose 的三个服务
里删掉，只留 POSTGRES_* 分量——于是 database_url 落回它的硬编码默认值
"sqlite+aiosqlite:///./metadata.db"：backend 连 PostgreSQL，两个 worker 却各自
连容器内一张表都没有的本地 sqlite 文件，第一个任务就会 "no such table"。

这里钉住修复：两个 worker 的引擎构造函数（_build_db_engine，从 startup() 中
抽出、避免测试要连 Redis/COS）在 postgres_host 被设置时必须产出 PostgreSQL
引擎，并套上 build_pool_kwargs 返回的连接池参数——而不是 SQLAlchemy 默认池。
"""

from sqlalchemy.pool import NullPool

from app.config import settings
from app.db import build_pool_kwargs
from worker import arq_worker, vc_arq_worker


def _set_pg_settings(monkeypatch):
    """把 settings 摆成「compose 只喂 POSTGRES_* 分量，DATABASE_URL 未设置」的
    Task 5 之后的真实状态——同时把 database_url 摆成它的 sqlite 默认回退值，
    确保测试在「读错字段」时会失败（而不是碰巧因为两个字段恰好相等而通过）。
    """
    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///./metadata.db")
    monkeypatch.setattr(settings, "postgres_host", "localhost")
    monkeypatch.setattr(settings, "postgres_port", 5433)
    monkeypatch.setattr(settings, "postgres_db", "videomaker_test")
    monkeypatch.setattr(settings, "postgres_user", "videomaker")
    monkeypatch.setattr(settings, "postgres_password", "devpassword")
    monkeypatch.setattr(settings, "postgres_password_file", "")


async def test_arq_worker_engine_uses_resolved_database_url(monkeypatch):
    _set_pg_settings(monkeypatch)
    assert settings.database_url.startswith("sqlite")  # 回退值仍是 sqlite——防止误判

    engine = arq_worker._build_db_engine()
    try:
        assert str(engine.url).startswith("postgresql+asyncpg://")
        assert engine.url.host == "localhost"
        assert engine.url.database == "videomaker_test"
    finally:
        await engine.dispose()


async def test_vc_arq_worker_engine_uses_resolved_database_url(monkeypatch):
    _set_pg_settings(monkeypatch)
    assert settings.database_url.startswith("sqlite")

    engine = vc_arq_worker._build_db_engine()
    try:
        assert str(engine.url).startswith("postgresql+asyncpg://")
        assert engine.url.host == "localhost"
        assert engine.url.database == "videomaker_test"
    finally:
        await engine.dispose()


async def test_arq_worker_engine_applies_postgres_pool_kwargs(monkeypatch):
    """postgres 分支必须套用 backend 同款连接池参数，而不是 SQLAlchemy 默认池。"""
    _set_pg_settings(monkeypatch)

    engine = arq_worker._build_db_engine()
    try:
        expected = build_pool_kwargs(settings.resolved_database_url)
        assert engine.pool.size() == expected["pool_size"]
        assert engine.pool._pre_ping is True
    finally:
        await engine.dispose()


async def test_vc_arq_worker_engine_applies_postgres_pool_kwargs(monkeypatch):
    _set_pg_settings(monkeypatch)

    engine = vc_arq_worker._build_db_engine()
    try:
        expected = build_pool_kwargs(settings.resolved_database_url)
        assert engine.pool.size() == expected["pool_size"]
        assert engine.pool._pre_ping is True
    finally:
        await engine.dispose()


async def test_arq_worker_engine_falls_back_to_sqlite_nullpool_without_pg_host(monkeypatch):
    """没设 postgres_host 时（本地开发/回滚场景），仍然落回 database_url + NullPool。"""
    monkeypatch.setattr(settings, "postgres_host", "")
    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///./metadata.db")

    engine = arq_worker._build_db_engine()
    try:
        assert str(engine.url).startswith("sqlite+aiosqlite://")
        assert isinstance(engine.pool, NullPool)
    finally:
        await engine.dispose()
