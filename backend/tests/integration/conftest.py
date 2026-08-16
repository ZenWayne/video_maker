"""Shared fixtures for backend integration tests."""
import subprocess
import tempfile
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from app.models.project import Project, Shot, ReferenceImage, ReferenceSample

# conftest_cos.py defines the cos_prefix fixture used by COS integration tests;
# it isn't named conftest.py so pytest won't auto-discover it on its own.
# Re-exporting the fixture into this (real) conftest's namespace registers it
# for the whole integration/ tree without the `pytest_plugins` mechanism,
# which pytest disallows in non-top-level conftest files.
from tests.integration.conftest_cos import cos_prefix  # noqa: F401

USER = "test-user"
HEADERS = {"X-User-Name": USER}

# 集成测试创建的 project id 注册表。seed_shot_with_source 等 helper 会把
# 素材发布到真实的 projects/<id>/ 前缀（不是 cos_prefix 的 test/ 前缀），
# 不清理就会在 dev bucket 里永久堆积——历史上攒出过 2893 个孤儿对象，
# 直接把孤儿巡检报告淹没。
_test_project_ids: list[str] = []


def register_test_project(project_id: str) -> str:
    _test_project_ids.append(project_id)
    return project_id


async def cleanup_test_project_prefixes() -> int:
    """删掉本次测试创建的所有 projects/<id>/ 前缀。返回删除的对象数。"""
    if not _test_project_ids:
        return 0
    from tests.integration.conftest_cos import _cos_configured
    if not _cos_configured():
        _test_project_ids.clear()
        return 0
    from app.services import cos_client, object_store
    await cos_client.warm_credentials()
    n = 0
    for pid in _test_project_ids:
        n += await object_store.delete_prefix(f"projects/{pid}/")
    _test_project_ids.clear()
    return n


@pytest.fixture(autouse=True)
async def _cleanup_cos_project_prefixes():
    _test_project_ids.clear()
    yield
    await cleanup_test_project_prefixes()


def install_fake_cos_credentials(monkeypatch):
    """Fake COS credentials so ``to_media_url()``/``object_store.signed_url()``
    can actually sign (rather than short-circuit on an absent/invalid key)
    without needing real COS credentials or network access.

    ``get_presigned_url`` is pure local HMAC computation — no network call —
    so a fake SecretId/SecretKey/Bucket produces a syntactically valid,
    deterministic URL. Use this for tests that assert on the *shape* of a
    signed URL (e.g. "starts with http, not /api/media/") without caring
    whether it is actually fetchable. Tests that need a REAL, fetchable URL
    must use the real ``cos_prefix`` fixture (+ ``@requires_cos``) instead —
    never this.

    Mirrors ``tests/unit/test_object_store_signed_url.py``'s
    ``_fake_static_client`` helper.
    """
    from app.config import settings
    from app.services import cos_client

    monkeypatch.setattr(settings, "cos_bucket", "fake-bucket-1250000000")
    monkeypatch.setattr(settings, "cos_region", "ap-guangzhou")
    monkeypatch.setattr(settings, "cos_scheme", "https")
    monkeypatch.setattr(settings, "cos_domain", None)
    monkeypatch.setattr(cos_client, "_client", None)
    monkeypatch.setattr(
        cos_client, "_cached_cred",
        {"secret_id": "fake-id", "secret_key": "fake-key", "token": None},
    )
    monkeypatch.setattr(cos_client, "_cred_expires_at", None)


@pytest.fixture
async def redis():
    import redis.asyncio as aioredis
    r = aioredis.from_url("redis://localhost:6381/15", decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest.fixture
async def client(db_engine, db_session_factory, redis, monkeypatch):
    # Import app.main first so all routers are fully loaded before we access submodules
    from app.main import app, get_redis
    from app.db import get_session
    import app.db as db_module
    import app.api.stream as stream_module
    import app.api.pipeline as pipeline_module
    import app.api.voice as voice_module
    import app.api.content_analysis as content_analysis_module

    # Override DB session factory everywhere
    monkeypatch.setattr(db_module, "AsyncSession", db_session_factory)
    monkeypatch.setattr(stream_module, "session_factory", db_session_factory)
    monkeypatch.setattr(content_analysis_module, "session_factory", db_session_factory)

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
    monkeypatch.setattr(content_analysis_module, "_get_arq_redis", _fake_get_arq)

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
        return register_test_project(p.id)


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


async def seed_analysis_sample_video(sf, analysis_id: str, sample_id: int, frames: int = 10) -> str:
    """Synthesize a real, tiny source video via ffmpeg, publish it to COS, and
    update the already-inserted ``ReferenceSample`` row's ``video_path``.

    Mirrors ``seed_shot_with_source`` for content-analysis samples: COS is the
    only storage, so worker.run_content_analysis's per-sample
    ``workspace().fetch(smp.video_path)`` needs a real object to download.

    Requires COS credentials to be warmed — depend on the ``cos_prefix``
    fixture (which warms them) and gate the test with ``requires_cos`` from
    ``tests.integration.conftest_cos``.
    """
    from app.services.storage import sample_video_key
    from app.services import object_store

    with tempfile.TemporaryDirectory() as td:
        local = Path(td) / "source.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=10",
                "-f", "lavfi", "-i", "sine=frequency=440",
                "-frames:v", str(frames),
                "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac",
                "-shortest", str(local),
            ],
            check=True,
            capture_output=True,
        )
        key = await object_store.put(sample_video_key(analysis_id, sample_id, "source.mp4"), local)

    async with sf() as s:
        smp = (await s.execute(
            select(ReferenceSample).where(ReferenceSample.id == sample_id)
        )).scalar_one()
        smp.video_path = key
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
        data = r.json()
        register_test_project(data["id"])
        return data
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
