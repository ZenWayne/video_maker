"""连接池选择逻辑：SQLite 用 NullPool，PostgreSQL 用带尺寸的 QueuePool。

背景：aiosqlite 的每个连接是独立的异步进程，池化没有收益，且 SSE 长连接
并发时 QueuePool 会被耗尽（见 db.py 历史注释）。PostgreSQL 相反——建连接
昂贵，必须池化。所以这里按 URL 前缀分流。
"""

from sqlalchemy.pool import NullPool

from app.config import settings
from app.db import build_pool_kwargs


def test_sqlite_url_uses_nullpool():
    kwargs = build_pool_kwargs("sqlite+aiosqlite:///./metadata.db")
    assert kwargs["poolclass"] is NullPool


def test_postgres_url_uses_sized_pool():
    kwargs = build_pool_kwargs("postgresql+asyncpg://u:p@h:5432/db")
    # 不指定 poolclass —— 让 SQLAlchemy 用默认的 QueuePool
    assert "poolclass" not in kwargs
    assert kwargs["pool_size"] == settings.db_pool_size
    assert kwargs["max_overflow"] == settings.db_max_overflow
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_recycle"] == settings.db_pool_recycle_sec


def test_resolved_url_falls_back_to_database_url_when_no_pg_host(monkeypatch):
    monkeypatch.setattr(settings, "postgres_host", "")
    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///./x.db")
    assert settings.resolved_database_url == "sqlite+aiosqlite:///./x.db"


def test_resolved_url_assembles_pg_url_from_parts(monkeypatch):
    """断言的是「URL 能原样解析回各个分量」，而不是某个具体的转义字符串。

    密码里的 @ 和空格若转义错了，URL 会在 userinfo 处断错位——但不同转义
    方案（%20 vs +）都可能是对的，写死字符串会变成脆断言。回环解析才是
    真正要保证的性质。
    """
    from sqlalchemy import make_url

    monkeypatch.setattr(settings, "postgres_host", "db.example")
    monkeypatch.setattr(settings, "postgres_port", 5432)
    monkeypatch.setattr(settings, "postgres_db", "videomaker")
    monkeypatch.setattr(settings, "postgres_user", "vm")
    monkeypatch.setattr(settings, "postgres_password", "p@ss word")
    monkeypatch.setattr(settings, "postgres_password_file", "")

    url = make_url(settings.resolved_database_url)
    assert url.drivername == "postgresql+asyncpg"
    assert url.username == "vm"
    assert url.password == "p@ss word"
    assert url.host == "db.example"
    assert url.port == 5432
    assert url.database == "videomaker"


def test_resolved_url_reads_password_from_file(monkeypatch, tmp_path):
    from sqlalchemy import make_url

    pw_file = tmp_path / "postgres_password"
    pw_file.write_text("filesecret\n")  # 末尾换行必须被 strip 掉
    monkeypatch.setattr(settings, "postgres_host", "db.example")
    monkeypatch.setattr(settings, "postgres_port", 5432)
    monkeypatch.setattr(settings, "postgres_db", "videomaker")
    monkeypatch.setattr(settings, "postgres_user", "vm")
    monkeypatch.setattr(settings, "postgres_password", "")
    monkeypatch.setattr(settings, "postgres_password_file", str(pw_file))

    assert make_url(settings.resolved_database_url).password == "filesecret"
