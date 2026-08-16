"""全局测试基建：所有测试跑在真实 PostgreSQL 上。

为什么不用 SQLite 内存库：计费账本依赖 PostgreSQL 的行级锁与事务语义，
而 SQLite 没有。测试若跑在 SQLite 上，就测不到我们真正部署的行为——
迁移的意义有一半在这里。

隔离策略：每个测试前 drop_all + create_all。表只有 7 张，成本约 50ms，
换来的是与生产完全一致的方言行为和零残留。

为什么测试用 create_all 而生产用 Alembic：两者都以 Base.metadata 为准，
而 test_alembic_schema.py 用 compare_metadata 断言「Alembic head == ORM
metadata」。有那道闸门在，create_all 建出的表就等价于 Alembic 建出的表，
测试因此既快又不会与生产漂移。闸门一旦被删，这个等价就不成立了。
"""

import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models.project import Base

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://videomaker:devpassword@localhost:5433/videomaker_test"
)


def get_test_database_url() -> str:
    """测试库 URL。用独立的 videomaker_test 库，绝不碰开发库 videomaker。"""
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


@pytest.fixture
async def db_engine():
    engine = create_async_engine(get_test_database_url(), poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session_factory(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
