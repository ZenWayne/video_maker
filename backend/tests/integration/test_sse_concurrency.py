"""SSE 并发不得耗尽连接池。

历史背景：SSE 流曾经在整个连接生命周期里持有 DB session，导致连接池被
耗尽（见 db.py 的历史注释）。现有代码已改为「快照查询后立即释放 session」，
本测试是迁移到 PostgreSQL + QueuePool 之后对该行为的回归钉子。

关键：这里**必须自建一个带生产连接池配置的引擎**，不能用 tests/conftest.py
的 db_engine —— 后者是 NullPool，池尺寸约束根本不生效，那样测的就不是
我们要验的东西了。用 build_pool_kwargs 拿到与生产完全相同的池配置：
pool_size=5 + max_overflow=10 = 最多 15 条连接，而我们要开 20 路并发。
只有 session 被及时归还，20 路才可能都跑完。
"""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import build_pool_kwargs
from app.models.project import Base, Project
from tests.conftest import get_test_database_url

CONCURRENCY = 20


@pytest.fixture
async def pooled_session_factory():
    """用生产同款 QueuePool 配置建引擎（区别于 db_engine 的 NullPool）。"""
    url = get_test_database_url()
    pool_kwargs = build_pool_kwargs(url)
    assert "poolclass" not in pool_kwargs, "测试库必须是 PostgreSQL，否则本测试无意义"
    assert pool_kwargs["pool_size"] + pool_kwargs["max_overflow"] < CONCURRENCY, (
        "并发数必须超过池上限，否则测不出 session 是否被及时归还"
    )

    engine = create_async_engine(url, **pool_kwargs)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    await engine.dispose()


@pytest.fixture
async def seeded_project(pooled_session_factory):
    async with pooled_session_factory() as s:
        p = Project(
            id="22222222-2222-2222-2222-222222222222",
            title="并发样例", theme_text="主题", creator_name="wayne",
            status="draft", aspect_ratio="9:16",
        )
        s.add(p)
        await s.commit()
        return p.id


async def test_twenty_concurrent_snapshot_queries(pooled_session_factory, seeded_project):
    async def snapshot() -> str:
        async with pooled_session_factory() as s:
            row = (await s.execute(
                select(Project).where(Project.id == seeded_project)
            )).scalar_one()
            return row.title

    results = await asyncio.wait_for(
        asyncio.gather(*(snapshot() for _ in range(CONCURRENCY))),
        timeout=30,
    )
    assert results == ["并发样例"] * CONCURRENCY
