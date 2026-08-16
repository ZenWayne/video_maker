import pytest
from sqlalchemy import select
from app.models.project import Project


@pytest.fixture
async def sf(db_session_factory):
    """沿用原名 sf，实际委托给 tests/conftest.py 的 PostgreSQL 会话工厂。"""
    return db_session_factory


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
