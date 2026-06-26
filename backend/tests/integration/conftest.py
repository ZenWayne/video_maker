"""Shared fixtures for backend integration tests."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.project import Base, Project, Shot, ReferenceImage

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
