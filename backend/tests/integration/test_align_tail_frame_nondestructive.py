"""Integration tests for non-destructive align-tail-frame endpoint (real COS).

POST /api/projects/{pid}/shots/{sid}/align-tail-frame must:
  - set shot.trim_frames in the DB (metadata only)
  - leave the source video object byte-identical in COS (never modified)
  - refresh last_frame and reset CC state
  - return aligned_to_frame == the mocked best frame value
"""
from unittest.mock import patch

from sqlalchemy import select

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import HEADERS, _make_project, _add_shot, seed_shot_with_source
from app.models.project import Shot
from app.services import object_store
from app.services.storage import shot_key

pytestmark = requires_cos


async def _seed_target_frame(pid: str, shot_id: int, tmp_path) -> str:
    key = shot_key(pid, shot_id, "target_last_frame.png")
    f = tmp_path / "target.png"
    f.write_bytes(b"fake-target-frame")
    await object_store.put(key, f)
    return key


async def test_align_tail_frame_metadata_only(
    client, db_session_factory, cos_prefix, tmp_path
):
    """align-tail-frame should only update trim_frames in DB; source object must
    stay byte-identical in COS."""
    pid = await _make_project(db_session_factory, status="completed")
    await _add_shot(db_session_factory, pid, shot_id=1, status="completed")
    source_key = await seed_shot_with_source(db_session_factory, pid, 1)
    target_key = await _seed_target_frame(pid, 1, tmp_path)

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.target_last_frame_path = target_key
        await s.commit()

    before = tmp_path / "before.mp4"
    await object_store.get(source_key, before)
    before_bytes = before.read_bytes()

    with patch("app.agents.video_trimmer.find_best_tail_frame", return_value=40):
        r = await client.post(
            f"/api/projects/{pid}/shots/1/align-tail-frame",
            headers=HEADERS,
        )

    assert r.status_code == 200
    body = r.json()
    assert body["trim_frames"] == 40
    assert body["aligned_to_frame"] == 40

    after = tmp_path / "after.mp4"
    await object_store.get(source_key, after)
    assert after.read_bytes() == before_bytes, "Source object was mutated — must be immutable"


async def test_align_tail_frame_resets_cc(client, db_session_factory, cos_prefix, tmp_path):
    """align-tail-frame must clear cc_status, clear pre_cc_last_frame_key (+ delete
    the backup object), and publish a new last_frame object."""
    pid = await _make_project(db_session_factory, status="completed")
    await _add_shot(db_session_factory, pid, shot_id=1, status="completed")
    await seed_shot_with_source(db_session_factory, pid, 1)
    target_key = await _seed_target_frame(pid, 1, tmp_path)

    pre_cc_key = f"projects/{pid}/shots/shot_1/last_frame_pre_cc.png"
    f = tmp_path / "pre_cc.png"
    f.write_bytes(b"fake-pre-cc")
    await object_store.put(pre_cc_key, f)

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.target_last_frame_path = target_key
        shot.cc_status = "done"
        shot.pre_cc_last_frame_key = pre_cc_key
        await s.commit()

    with patch("app.agents.video_trimmer.find_best_tail_frame", return_value=40):
        r = await client.post(
            f"/api/projects/{pid}/shots/1/align-tail-frame",
            headers=HEADERS,
        )
    assert r.status_code == 200

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        assert shot.cc_status is None
        assert shot.trim_frames == 40
        assert shot.last_frame_path is not None
        assert await object_store.exists(shot.last_frame_path)
        assert shot.pre_cc_last_frame_key is None

    assert await object_store.exists(pre_cc_key) is False


async def test_align_tail_frame_no_target_returns_400(client, db_session_factory, cos_prefix):
    """Without target_last_frame_path, endpoint must return 400."""
    pid = await _make_project(db_session_factory, status="completed")
    await _add_shot(db_session_factory, pid, shot_id=1, status="completed")
    await seed_shot_with_source(db_session_factory, pid, 1)

    r = await client.post(
        f"/api/projects/{pid}/shots/1/align-tail-frame",
        headers=HEADERS,
    )
    assert r.status_code == 400
