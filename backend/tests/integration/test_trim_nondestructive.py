"""Integration tests for non-destructive trim endpoint (real COS).

POST /api/projects/{pid}/shots/{sid}/trim must:
  - set shot.trim_frames = end_frame in the DB
  - leave the source video object byte-identical in COS (never modified)
  - refresh last_frame (a fresh COS object) and reset CC state
"""
from sqlalchemy import select

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import HEADERS, _make_project, _add_shot, seed_shot_with_source
from app.models.project import Shot
from app.services import object_store

pytestmark = requires_cos


async def test_trim_sets_metadata_and_keeps_source_immutable(
    client, db_session_factory, cos_prefix, tmp_path
):
    """Trimming should only update trim_frames in the DB; the source COS object
    must be byte-identical before and after."""
    pid = await _make_project(db_session_factory, status="completed")
    await _add_shot(db_session_factory, pid, shot_id=1, status="completed")
    source_key = await seed_shot_with_source(db_session_factory, pid, 1)

    before = tmp_path / "before.mp4"
    await object_store.get(source_key, before)
    before_bytes = before.read_bytes()

    r = await client.post(
        f"/api/projects/{pid}/shots/1/trim",
        json={"end_frame": 40},
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["trim_frames"] == 40

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
    assert shot.video_path == source_key, "video_path must still point at the source key"

    after = tmp_path / "after.mp4"
    await object_store.get(source_key, after)
    assert after.read_bytes() == before_bytes, "Source object was mutated — must be immutable"


async def test_trim_resets_cc_and_refreshes_last_frame(
    client, db_session_factory, cos_prefix, tmp_path
):
    """Trim must clear cc_status, clear pre_cc_last_frame_key (+ delete the backup
    object), and publish a new last_frame object."""
    pid = await _make_project(db_session_factory, status="completed")
    await _add_shot(db_session_factory, pid, shot_id=1, status="completed")
    await seed_shot_with_source(db_session_factory, pid, 1)

    # Seed a fake pre-CC backup object and set cc_status to simulate a prior CC run
    pre_cc_key = f"projects/{pid}/shots/shot_1/last_frame_pre_cc.png"
    f = tmp_path / "pre_cc.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"pre-cc")
    await object_store.put(pre_cc_key, f)
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.cc_status = "done"
        shot.pre_cc_last_frame_key = pre_cc_key
        await s.commit()

    r = await client.post(
        f"/api/projects/{pid}/shots/1/trim",
        json={"end_frame": 40},
        headers=HEADERS,
    )
    assert r.status_code == 200

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        assert shot.cc_status is None
        assert shot.trim_frames == 40
        # last_frame_path should point to a new, real COS object
        assert shot.last_frame_path is not None
        assert await object_store.exists(shot.last_frame_path)
        assert shot.pre_cc_last_frame_key is None

    # pre-CC backup object must be deleted (DB de-referenced first, then COS delete)
    assert await object_store.exists(pre_cc_key) is False


async def test_trim_below_min_frames_rejected(client, db_session_factory, cos_prefix):
    """end_frame < 24 must return 400."""
    pid = await _make_project(db_session_factory, status="completed")
    await _add_shot(db_session_factory, pid, shot_id=1, status="completed")
    await seed_shot_with_source(db_session_factory, pid, 1)

    r = await client.post(
        f"/api/projects/{pid}/shots/1/trim",
        json={"end_frame": 10},
        headers=HEADERS,
    )
    assert r.status_code == 400


async def test_trim_video_path_still_points_to_source(
    client, db_session_factory, cos_prefix
):
    """After trim, shot.video_path must still point to the original source key."""
    pid = await _make_project(db_session_factory, status="completed")
    await _add_shot(db_session_factory, pid, shot_id=1, status="completed")
    source_key = await seed_shot_with_source(db_session_factory, pid, 1)

    r = await client.post(
        f"/api/projects/{pid}/shots/1/trim",
        json={"end_frame": 40},
        headers=HEADERS,
    )
    assert r.status_code == 200

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        assert shot.video_path == source_key, (
            f"video_path changed from source: {shot.video_path} != {source_key}"
        )


async def test_restore_clears_trim(client, db_session_factory, cos_prefix, tmp_path):
    """restore-trim must clear trim_frames and leave the source object byte-identical."""
    pid = await _make_project(db_session_factory, status="completed")
    await _add_shot(db_session_factory, pid, shot_id=1, status="completed")
    source_key = await seed_shot_with_source(db_session_factory, pid, 1)

    before = tmp_path / "before.mp4"
    await object_store.get(source_key, before)
    before_bytes = before.read_bytes()

    # Apply a trim first
    r = await client.post(
        f"/api/projects/{pid}/shots/1/trim",
        json={"end_frame": 40},
        headers=HEADERS,
    )
    assert r.status_code == 200

    # Now restore
    r = await client.post(
        f"/api/projects/{pid}/shots/1/restore-trim",
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["trim_frames"] is None

    # Source object must be byte-identical across the whole cycle
    after = tmp_path / "after.mp4"
    await object_store.get(source_key, after)
    assert after.read_bytes() == before_bytes, "Source object was mutated — must be immutable"
