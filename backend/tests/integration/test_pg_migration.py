"""SQLite → PostgreSQL 数据搬迁。

两个必须验证的点：
1. 按外键顺序搬，否则子表先插会违反外键约束。
2. PostgreSQL 的自增序列（SERIAL）必须在搬完后 setval 对齐，否则下一次
   INSERT 会从 1 开始，撞上已搬进来的主键——这是跨库搬迁最经典的坑。
"""

import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select, text
from sqlalchemy.pool import NullPool

from app.models.project import Base, Event, Project, Shot
from app.scripts.pg_migration.migrate import TABLE_ORDER, copy_all


def _pg_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://videomaker:devpassword@localhost:5433/videomaker_test",
    )


@pytest.fixture
async def sqlite_src(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'src.db'}"
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        p = Project(
            id="11111111-1111-1111-1111-111111111111",
            title="搬迁样例", theme_text="主题", creator_name="wayne",
            status="draft", aspect_ratio="9:16",
        )
        s.add(p)
        await s.flush()
        s.add(Shot(
            project_id=p.id, shot_id=1, text="台词", shot_type="Close-up",
            visual_description="描述", shot_duration=8, status="pending",
        ))
        s.add(Event(project_id=p.id, actor="user:wayne", event_type="created"))
        await s.commit()
    yield url
    await engine.dispose()


def test_table_order_puts_parents_before_children():
    assert TABLE_ORDER.index("projects") < TABLE_ORDER.index("shots")
    assert TABLE_ORDER.index("projects") < TABLE_ORDER.index("events")
    assert TABLE_ORDER.index("shots") < TABLE_ORDER.index("image_candidates")
    assert TABLE_ORDER.index("content_analyses") < TABLE_ORDER.index("reference_samples")


async def test_copies_all_rows(sqlite_src):
    dst = create_async_engine(_pg_url(), poolclass=NullPool)
    async with dst.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await dst.dispose()

    counts = await copy_all(sqlite_src, _pg_url())
    assert counts["projects"] == 1
    assert counts["shots"] == 1
    assert counts["events"] == 1

    engine = create_async_engine(_pg_url(), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        rows = (await s.execute(select(Project))).scalars().all()
        assert len(rows) == 1
        assert rows[0].title == "搬迁样例"
    await engine.dispose()


async def test_sequences_are_realigned_after_copy(sqlite_src):
    """搬完之后插新行，主键不能撞上已搬进来的。"""
    dst = create_async_engine(_pg_url(), poolclass=NullPool)
    async with dst.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await dst.dispose()

    await copy_all(sqlite_src, _pg_url())

    engine = create_async_engine(_pg_url(), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        # 序列没对齐的话，这一插会因主键冲突而失败
        s.add(Event(
            project_id="11111111-1111-1111-1111-111111111111",
            actor="system:worker", event_type="after_migration",
        ))
        await s.commit()
        total = (await s.execute(text("SELECT count(*) FROM events"))).scalar()
        assert total == 2
    await engine.dispose()
