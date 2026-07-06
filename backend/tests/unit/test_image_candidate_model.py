"""ImageCandidate 模型 + 序列化 + candidates 目录 helper."""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.project import Base, Project, Shot


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


async def _seed(sf):
    async with sf() as s:
        p = Project(title="t", theme_text="t", creator_name="u", status="shot_review")
        s.add(p)
        await s.flush()
        shot = Shot(
            project_id=p.id, shot_id=1, text="hi", shot_type="Medium Shot",
            visual_description="v", shot_duration=6, status="completed",
        )
        s.add(shot)
        await s.commit()
        return p.id, shot.id


@pytest.mark.asyncio
async def test_candidate_roundtrip_and_defaults(sf):
    from app.models.project import ImageCandidate
    pid, shot_pk = await _seed(sf)
    async with sf() as s:
        c = ImageCandidate(project_id=pid, shot_pk=shot_pk, shot_id=1, slot="tail_frame")
        s.add(c)
        await s.commit()
        row = (await s.execute(select(ImageCandidate))).scalar_one()
        assert row.status == "generating"
        assert row.prompt_source == "auto"
        assert row.adopted_at is None
        assert len(row.id) == 36


@pytest.mark.asyncio
async def test_shot_relationship_selectin_and_cascade(sf):
    from app.models.project import ImageCandidate
    pid, shot_pk = await _seed(sf)
    async with sf() as s:
        s.add(ImageCandidate(project_id=pid, shot_pk=shot_pk, shot_id=1, slot="cc"))
        await s.commit()
    async with sf() as s:
        shot = (await s.execute(select(Shot).where(Shot.id == shot_pk))).scalar_one()
        assert len(shot.image_candidates) == 1  # lazy="selectin" 自动加载
        await s.delete(shot)
        await s.commit()
        assert (await s.execute(select(ImageCandidate))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_shot_to_dict_includes_candidates(sf):
    from app.models.project import ImageCandidate
    from app.api.projects import _shot_to_dict
    pid, shot_pk = await _seed(sf)
    async with sf() as s:
        s.add(ImageCandidate(
            project_id=pid, shot_pk=shot_pk, shot_id=1, slot="tail_frame",
            status="done", file_path="/nonstorage/x.png", custom_prompt="p",
            prompt_source="custom",
        ))
        await s.commit()
        shot = (await s.execute(select(Shot).where(Shot.id == shot_pk))).scalar_one()
        d = _shot_to_dict(shot)
        assert len(d["image_candidates"]) == 1
        c = d["image_candidates"][0]
        assert c["slot"] == "tail_frame"
        assert c["status"] == "done"
        assert c["prompt_source"] == "custom"
        assert c["custom_prompt"] == "p"
        assert "id" in c and "created_at" in c and "adopted_at" in c


def test_shot_candidates_dir():
    from app.services.storage import shot_candidates_dir, shot_dir
    assert shot_candidates_dir("pid", 3) == shot_dir("pid", 3) / "candidates"
