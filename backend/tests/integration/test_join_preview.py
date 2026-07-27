"""连贯性预览端点：real COS.

Rewritten for Task 11 (join-preview moved from local-storage-root paths to
workspace()+COS). Gated on requires_cos/cos_prefix — seed_shot_with_source and
the endpoint itself both do real COS I/O.
"""
import pytest
from sqlalchemy import select

from app.models.project import Shot
from app.services import object_store
from app.services.storage import shot_key, join_preview_key
from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import HEADERS, _make_project, _add_shot, seed_shot_with_source

pytestmark = requires_cos


@pytest.mark.asyncio
async def test_join_preview_success(client, db_session_factory, cos_prefix):
    pid = await _make_project(db_session_factory, status="shot_review")
    for i in (1, 2, 3):
        await _add_shot(db_session_factory, pid, i, status="completed")
        await seed_shot_with_source(db_session_factory, pid, i, frames=30)

    r = await client.post(
        f"/api/projects/{pid}/join-preview",
        json={"shot_ids": [2, 3]},
        headers=HEADERS,
    )

    assert r.status_code == 200, r.text
    url = r.json()["preview_url"]
    # cache-busting query param, and the URL is a signed COS URL for the
    # canonical join-preview key (not a local /api/media/ path anymore).
    assert "?t=" in url
    key = join_preview_key(pid)
    assert await object_store.exists(key)
    assert await object_store.size(key) > 0


@pytest.mark.asyncio
async def test_join_preview_requires_two_shots(client, db_session_factory, cos_prefix):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="completed")
    await seed_shot_with_source(db_session_factory, pid, 1, frames=30)

    r = await client.post(
        f"/api/projects/{pid}/join-preview",
        json={"shot_ids": [1]},
        headers=HEADERS,
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_join_preview_rejects_incomplete_shot(client, db_session_factory, cos_prefix):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="completed")
    await seed_shot_with_source(db_session_factory, pid, 1, frames=30)
    # shot 2 是 pending、无 video_path
    await _add_shot(db_session_factory, pid, 2, status="pending")

    r = await client.post(
        f"/api/projects/{pid}/join-preview",
        json={"shot_ids": [1, 2]},
        headers=HEADERS,
    )
    assert r.status_code == 400
    assert "2" in r.json()["detail"]


@pytest.mark.asyncio
async def test_join_preview_rejects_missing_video_file(client, db_session_factory, cos_prefix):
    pid = await _make_project(db_session_factory, status="shot_review")
    # shot 1: 正常的 completed shot，带真实 COS 视频
    await _add_shot(db_session_factory, pid, 1, status="completed")
    await seed_shot_with_source(db_session_factory, pid, 1, frames=30)
    # shot 2: completed 但 video_path 指向一个不存在的 COS key
    await _add_shot(db_session_factory, pid, 2, status="completed")
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 2)
        )).scalar_one()
        shot.video_path = shot_key(pid, 2, "nonexistent.mp4")
        await s.commit()

    r = await client.post(
        f"/api/projects/{pid}/join-preview",
        json={"shot_ids": [1, 2]},
        headers=HEADERS,
    )
    assert r.status_code == 400
    assert "2" in r.json()["detail"]
