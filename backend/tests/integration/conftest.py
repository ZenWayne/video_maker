"""Shared fixtures for backend integration tests."""
import subprocess
import tempfile
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.project import Base, Project, Shot, ReferenceImage

# conftest_cos.py defines the cos_prefix fixture used by COS integration tests;
# it isn't named conftest.py so pytest won't auto-discover it on its own.
# Re-exporting the fixture into this (real) conftest's namespace registers it
# for the whole integration/ tree without the `pytest_plugins` mechanism,
# which pytest disallows in non-top-level conftest files.
from tests.integration.conftest_cos import cos_prefix  # noqa: F401

USER = "test-user"
HEADERS = {"X-User-Name": USER}


@pytest.fixture
async def db_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session_factory(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def redis():
    import redis.asyncio as aioredis
    r = aioredis.from_url("redis://localhost:6381/15", decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest.fixture
async def client(db_engine, db_session_factory, redis, monkeypatch, tmp_path):
    # Import app.main first so all routers are fully loaded before we access submodules
    from app.main import app, get_redis
    from app.db import get_session
    import app.db as db_module
    import app.api.stream as stream_module
    import app.api.pipeline as pipeline_module
    import app.api.voice as voice_module
    from app.config import settings

    # Override DB session factory everywhere
    monkeypatch.setattr(db_module, "AsyncSession", db_session_factory)
    monkeypatch.setattr(stream_module, "session_factory", db_session_factory)

    # Override storage root so file ops use tmp_path
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))

    # Mock ARQ to prevent actual job execution (would trigger LLM calls)
    arq = MagicMock()
    arq.enqueue_job = AsyncMock(return_value=None)

    async def _fake_get_arq(_redis):
        return arq

    monkeypatch.setattr(pipeline_module, "_get_arq_redis", _fake_get_arq)
    # voice routes were extracted to app.api.voice; they bind _get_arq_redis in
    # their own namespace, so patch it there too (per-namespace mocking).
    monkeypatch.setattr(voice_module, "_get_arq_redis", _fake_get_arq)
    import app.api.image_candidates as image_candidates_module
    monkeypatch.setattr(image_candidates_module, "_get_arq_redis", _fake_get_arq)

    # Override FastAPI DI
    async def override_session():
        async with db_session_factory() as s:
            yield s

    async def override_redis():
        return redis

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_redis] = override_redis

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.arq = arq
        yield c

    app.dependency_overrides.clear()


# ── DB helpers ─────────────────────────────────────────────────────────────────

async def _make_project(sf, status="draft", scene_overview=None):
    async with sf() as s:
        p = Project(
            title="Test Project",
            theme_text="Test theme",
            creator_name=USER,
            status=status,
            scene_overview=scene_overview,
        )
        s.add(p)
        await s.commit()
        await s.refresh(p)
        return p.id


async def _add_shots(sf, project_id, count=3, status="completed"):
    async with sf() as s:
        for i in range(1, count + 1):
            s.add(Shot(
                project_id=project_id,
                shot_id=i,
                text=f"Shot {i} dialogue",
                shot_type="Medium Shot",
                visual_description=f"Visual description {i}",
                shot_duration=6,
                status=status,
                align_with_previous=(i > 1),
            ))
        await s.commit()


async def _add_shot(sf, project_id, shot_id, status="completed"):
    async with sf() as s:
        s.add(Shot(
            project_id=project_id,
            shot_id=shot_id,
            text=f"Shot {shot_id} dialogue",
            shot_type="Medium Shot",
            visual_description=f"Visual description {shot_id}",
            shot_duration=6,
            status=status,
            align_with_previous=(shot_id > 1),
        ))
        await s.commit()


async def _add_character_image(sf, project_id):
    async with sf() as s:
        img = ReferenceImage(
            project_id=project_id,
            kind="character",
            filename="test.jpg",
            storage_path=f"/fake/{project_id}/test.jpg",
            order_index=0,
        )
        s.add(img)
        await s.commit()
        await s.refresh(img)
        return img.id


# ── Shared seeding helper (Tasks 5/6/7/8/9) ────────────────────────────────────

async def seed_shot_with_source(sf, project_id: str, shot_id: int, frames: int = 120) -> str:
    """Synthesize a real source video via ffmpeg, publish it to COS, and update the
    already-inserted shot row's ``video_path``/``source_fps``/``source_frames``.

    Returns the COS **key** the video was published under (COS is the only
    storage — there is no local "shot dir" anymore). Callers that need to
    verify byte-for-byte immutability should ``object_store.get`` the key
    before/after and compare bytes, rather than touching a local Path.

    Requires COS credentials to be warmed — depend on the ``cos_prefix``
    fixture (which warms them) and gate the test with ``requires_cos`` from
    ``tests.integration.conftest_cos``.

    Reusable by Tasks 8/9/... integration tests.
    """
    from app.services.storage import shot_key, ts_uuid_name
    from app.services import object_store
    from app.agents.video_trimmer import get_video_info

    with tempfile.TemporaryDirectory() as td:
        local = Path(td) / f"output_{ts_uuid_name('.mp4')}"
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440",
                "-frames:v", str(frames),
                "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac",
                "-shortest", str(local),
            ],
            check=True,
            capture_output=True,
        )
        info = get_video_info(str(local))
        key = await object_store.put(shot_key(project_id, shot_id, local.name), local)

    async with sf() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
        )).scalar_one()
        shot.video_path = key
        shot.source_fps = info["fps"]
        shot.source_frames = info["total_frames"]
        await s.commit()
    return key


# ── State fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
async def make_project(client):
    async def _make(title="Test Project", theme="Test theme"):
        r = await client.post(
            "/api/projects",
            json={"title": title, "theme_text": theme},
            headers=HEADERS,
        )
        assert r.status_code == 201
        return r.json()
    return _make


@pytest.fixture
async def project_in_draft(make_project):
    return await make_project()


@pytest.fixture
async def project_in_draft_with_image(db_session_factory, project_in_draft):
    image_id = await _add_character_image(db_session_factory, project_in_draft["id"])
    return {"project": project_in_draft, "image_id": image_id}


@pytest.fixture
async def project_in_script_review(db_session_factory):
    pid = await _make_project(
        db_session_factory,
        status="script_review",
        scene_overview="Scene overview text",
    )
    await _add_shots(db_session_factory, pid, count=3, status="pending")
    return pid


@pytest.fixture
async def project_in_shot_review(db_session_factory):
    pid = await _make_project(
        db_session_factory,
        status="shot_review",
        scene_overview="Scene overview text",
    )
    await _add_shots(db_session_factory, pid, count=3, status="completed")
    return pid
