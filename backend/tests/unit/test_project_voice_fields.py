import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy import select
from app.models.project import Base, Project


@pytest.fixture
async def sf():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    await engine.dispose()


async def test_new_voice_fields_default(sf):
    async with sf() as s:
        p = Project(title="t", theme_text="th", creator_name="u")
        s.add(p)
        await s.commit()
        await s.refresh(p)
        assert p.reference_voice_path is None
        assert p.auto_voice_calibrate is False


async def test_new_voice_fields_roundtrip(sf):
    async with sf() as s:
        p = Project(title="t", theme_text="th", creator_name="u",
                    reference_voice_path="/x/prompt.wav", auto_voice_calibrate=True)
        s.add(p)
        await s.commit()
        pid = p.id
    async with sf() as s:
        got = (await s.execute(select(Project).where(Project.id == pid))).scalar_one()
        assert got.reference_voice_path == "/x/prompt.wav"
        assert got.auto_voice_calibrate is True
