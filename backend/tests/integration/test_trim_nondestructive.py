"""Integration tests for non-destructive trim endpoint.

POST /api/projects/{pid}/shots/{sid}/trim must:
  - set shot.trim_frames = end_frame in the DB
  - leave the source output_*.mp4 byte-identical (never modified)
  - NOT create any trimmed_*.mp4 file
  - refresh last_frame and reset CC state
"""
import hashlib
from pathlib import Path

import pytest

from .conftest import HEADERS, _make_project, _add_shot, seed_shot_with_source


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_trim_sets_metadata_and_keeps_source_immutable(
    client, db_session_factory
):
    """Trimming should only update trim_frames in the DB; source file must be unchanged."""
    pid = await _make_project(db_session_factory, status="completed")
    await _add_shot(db_session_factory, pid, shot_id=1, status="completed")
    source = await seed_shot_with_source(db_session_factory, pid, 1)

    before_md5 = _md5(source)

    r = await client.post(
        f"/api/projects/{pid}/shots/1/trim",
        json={"end_frame": 40},
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["trim_frames"] == 40

    # Source video must be byte-identical
    assert _md5(source) == before_md5, "Source file was mutated — must be immutable"

    # No trimmed_ files must have been created
    assert not list(source.parent.glob("trimmed_*.mp4")), "trimmed_*.mp4 was created"


@pytest.mark.asyncio
async def test_trim_resets_cc_and_refreshes_last_frame(
    client, db_session_factory
):
    """Trim must clear cc_status and write a new last_frame_*.png."""
    from sqlalchemy import select
    from app.models.project import Shot

    pid = await _make_project(db_session_factory, status="completed")
    await _add_shot(db_session_factory, pid, shot_id=1, status="completed")
    source = await seed_shot_with_source(db_session_factory, pid, 1)

    # Seed a fake pre-CC file and set cc_status to simulate prior CC run
    pre_cc = source.parent / "last_frame_pre_cc.png"
    pre_cc.write_bytes(b"fake")
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.cc_status = "done"
        await s.commit()

    r = await client.post(
        f"/api/projects/{pid}/shots/1/trim",
        json={"end_frame": 40},
        headers=HEADERS,
    )
    assert r.status_code == 200

    # CC state cleared
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        assert shot.cc_status is None
        assert shot.trim_frames == 40
        # last_frame_path should point to a new file
        assert shot.last_frame_path is not None
        assert Path(shot.last_frame_path).exists()

    # pre-CC backup must be deleted
    assert not pre_cc.exists(), "last_frame_pre_cc.png should have been removed"


@pytest.mark.asyncio
async def test_trim_below_min_frames_rejected(client, db_session_factory):
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


@pytest.mark.asyncio
async def test_trim_video_path_still_points_to_source(client, db_session_factory):
    """After trim, shot.video_path must still point to the original output_*.mp4."""
    from sqlalchemy import select
    from app.models.project import Shot

    pid = await _make_project(db_session_factory, status="completed")
    await _add_shot(db_session_factory, pid, shot_id=1, status="completed")
    source = await seed_shot_with_source(db_session_factory, pid, 1)

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
        assert shot.video_path == str(source), (
            f"video_path changed from source: {shot.video_path} != {source}"
        )


@pytest.mark.asyncio
async def test_restore_clears_trim(client, db_session_factory):
    """restore-trim must clear trim_frames and leave the source file byte-identical."""
    pid = await _make_project(db_session_factory, status="completed")
    await _add_shot(db_session_factory, pid, shot_id=1, status="completed")
    source = await seed_shot_with_source(db_session_factory, pid, 1)

    before_md5 = _md5(source)

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

    # Source file must be byte-identical across the whole cycle
    assert _md5(source) == before_md5, "Source file was mutated — must be immutable"
