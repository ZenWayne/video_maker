"""Integration tests for DELETE tail-frame endpoint.

Covers Task 4 requirements:
- Clears target_last_frame_path + tf_status on DELETE
- Unlinks the DB-stored file path (not the hardcoded canonical path)
- Returns 409 when tf_status=="generating"
"""
import pytest
from pathlib import Path
from sqlalchemy import select

from tests.integration.conftest import HEADERS, _make_project
from app.models.project import Shot


async def _seed_shot_with_tail_frame(db_session_factory, project_id, file_path: str):
    """Create a shot that already has a tail frame stored at file_path."""
    async with db_session_factory() as s:
        shot = Shot(
            project_id=project_id,
            shot_id=1,
            text="Test shot",
            shot_type="Medium Shot",
            visual_description="Test visual",
            shot_duration=6,
            status="completed",
            align_with_previous=False,
            tf_status="done",
            tf_confirmed=True,
            target_last_frame_path=file_path,
        )
        s.add(shot)
        await s.commit()


async def _get_shot(db_session_factory, project_id, shot_id=1):
    async with db_session_factory() as s:
        result = await s.execute(
            select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
        )
        return result.scalar_one()


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_delete_tail_frame_clears_path_and_status(
    client, db_session_factory, tmp_path
):
    """DELETE returns 200, clears target_last_frame_path and tf_status in response."""
    pid = await _make_project(db_session_factory, status="shot_review")

    # Create a real file under tmp_path (storage root is patched to tmp_path in conftest)
    tail_file = tmp_path / "tail_frame_abc123.png"
    tail_file.write_bytes(b"\x89PNG\r\n")

    await _seed_shot_with_tail_frame(db_session_factory, pid, str(tail_file))

    r = await client.post(
        f"/api/projects/{pid}/shots/1/delete-tail-frame",
        headers=HEADERS,
    )
    assert r.status_code == 200
    data = r.json()
    # 1. Response: target_last_frame_path is None, tf_status is None
    assert data["target_last_frame_path"] is None
    assert data["tf_status"] is None


async def test_delete_tail_frame_clears_db(client, db_session_factory, tmp_path):
    """DELETE clears target_last_frame_path and tf_status in the database."""
    pid = await _make_project(db_session_factory, status="shot_review")

    tail_file = tmp_path / "tail_frame_xyz.png"
    tail_file.write_bytes(b"\x89PNG\r\n")

    await _seed_shot_with_tail_frame(db_session_factory, pid, str(tail_file))

    await client.post(
        f"/api/projects/{pid}/shots/1/delete-tail-frame",
        headers=HEADERS,
    )

    # 2. DB: target_last_frame_path is None, tf_status is None
    shot = await _get_shot(db_session_factory, pid)
    assert shot.target_last_frame_path is None
    assert shot.tf_status is None


async def test_delete_tail_frame_removes_file(client, db_session_factory, tmp_path):
    """DELETE removes the actual file whose path was stored in DB."""
    pid = await _make_project(db_session_factory, status="shot_review")

    tail_file = tmp_path / "tail_frame_to_delete.png"
    tail_file.write_bytes(b"\x89PNG\r\n")
    assert tail_file.exists()

    await _seed_shot_with_tail_frame(db_session_factory, pid, str(tail_file))

    await client.post(
        f"/api/projects/{pid}/shots/1/delete-tail-frame",
        headers=HEADERS,
    )

    # 4. The temp file must be removed from disk
    assert not tail_file.exists()


async def test_delete_tail_frame_blocks_when_generating(
    client, db_session_factory, tmp_path
):
    """DELETE returns 409 when tf_status=='generating' and does NOT clear the path."""
    pid = await _make_project(db_session_factory, status="shot_review")

    tail_file = tmp_path / "tail_frame_in_progress.png"
    tail_file.write_bytes(b"\x89PNG\r\n")

    # Seed with tf_status="generating" (in-flight)
    async with db_session_factory() as s:
        shot = Shot(
            project_id=pid,
            shot_id=1,
            text="Test shot",
            shot_type="Medium Shot",
            visual_description="Test visual",
            shot_duration=6,
            status="pending",
            align_with_previous=False,
            tf_status="generating",
            tf_confirmed=False,
            target_last_frame_path=str(tail_file),
        )
        s.add(shot)
        await s.commit()

    r = await client.post(
        f"/api/projects/{pid}/shots/1/delete-tail-frame",
        headers=HEADERS,
    )

    # 5. Guard: 409 when generating, path NOT cleared
    assert r.status_code == 409
    shot = await _get_shot(db_session_factory, pid)
    assert shot.target_last_frame_path == str(tail_file)
