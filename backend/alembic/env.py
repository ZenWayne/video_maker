"""Alembic 运行环境（async）。

数据库 URL 优先取调用方显式设到 Config 上的值（例如测试用 command API
按需指向 videomaker_test）；没有显式设置时，才回退到
app.config.settings.resolved_database_url——保证在没有更具体来源的情况下，
应用与迁移永远指向同一个库，不会出现「应用连 A、迁移改 B」。
"""

import asyncio
from logging.config import fileConfig
from pathlib import Path
import sys

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# 让 `alembic` 命令在 backend/ 目录下也能 import 到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.models.project import Base  # noqa: E402

config = context.config
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", settings.resolved_database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # 让 autogenerate 能发现列类型变化，否则改类型不会被检测到
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _with_async_driver(url: str) -> str:
    """Config 上的 URL 可能是不带异步驱动后缀的 plain ``postgresql://``
    （例如测试按 psycopg2 习惯显式设置的同步风格 URL——见
    ``tests/integration/test_alembic_schema.py``）。run_migrations_online
    全程走异步引擎，这里统一补上 ``+asyncpg``，不管 URL 来自
    settings.resolved_database_url（已经是 asyncpg）还是调用方显式设置
    （可能没写 driver）。
    """
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


async def run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _with_async_driver(section["sqlalchemy.url"])
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
