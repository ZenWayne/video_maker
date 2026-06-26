"""Integration tests for DELETE first-frame endpoint (Task 6).

Covers Task 6 requirements:
- Clears custom_first_frame_path on DELETE
- Unlinks the DB-stored file path (removes the actual file)
- Returns 404 when shot missing
- Idempotent when custom_first_frame_path is already None
"""
import pytest
from pathlib import Path
from sqlalchemy import select

from tests.integration.conftest import HEADERS, _make_project
from app.models.project import Shot


async def _seed_shot_with_first_frame(db_session_factory, project_id, file_path: str):
    """Create a shot that already has a custom first frame stored at file_path."""
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
            custom_first_frame_path=file_path,
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


async def test_delete_first_frame_clears_path_in_response(
    client, db_session_factory, tmp_path
):
    """DELETE returns 200, custom_first_frame_path is None in response."""
    pid = await _make_project(db_session_factory, status="shot_review")

    # Create a real file under tmp_path
    first_file = tmp_path / "first_frame_abc123.png"
    first_file.write_bytes(b"\x89PNG\r\n")

    await _seed_shot_with_first_frame(db_session_factory, pid, str(first_file))

    r = await client.delete(
        f"/api/projects/{pid}/shots/1/first-frame",
        headers=HEADERS,
    )
    assert r.status_code == 200
    data = r.json()
    # 1. Response: custom_first_frame_path is None
    assert data["custom_first_frame_path"] is None


async def test_delete_first_frame_clears_db(client, db_session_factory, tmp_path):
    """DELETE clears custom_first_frame_path in the database."""
    pid = await _make_project(db_session_factory, status="shot_review")

    first_file = tmp_path / "first_frame_xyz.png"
    first_file.write_bytes(b"\x89PNG\r\n")

    await _seed_shot_with_first_frame(db_session_factory, pid, str(first_file))

    await client.delete(
        f"/api/projects/{pid}/shots/1/first-frame",
        headers=HEADERS,
    )

    # 2. DB: custom_first_frame_path is None
    shot = await _get_shot(db_session_factory, pid)
    assert shot.custom_first_frame_path is None


async def test_delete_first_frame_removes_file(client, db_session_factory, tmp_path):
    """DELETE removes the actual file whose path was stored in DB."""
    pid = await _make_project(db_session_factory, status="shot_review")

    first_file = tmp_path / "first_frame_to_delete.png"
    first_file.write_bytes(b"\x89PNG\r\n")
    assert first_file.exists()

    await _seed_shot_with_first_frame(db_session_factory, pid, str(first_file))

    await client.delete(
        f"/api/projects/{pid}/shots/1/first-frame",
        headers=HEADERS,
    )

    # 3. The temp file must be removed from disk
    assert not first_file.exists()


async def test_delete_first_frame_shot_not_found(client, db_session_factory):
    """DELETE returns 404 when the shot doesn't exist."""
    pid = await _make_project(db_session_factory, status="shot_review")

    r = await client.delete(
        f"/api/projects/{pid}/shots/999/first-frame",
        headers=HEADERS,
    )

    # 4. Returns 404 when shot missing
    assert r.status_code == 404


async def test_delete_first_frame_idempotent(client, db_session_factory):
    """DELETE is idempotent: deleting when custom_first_frame_path is already None returns 200."""
    pid = await _make_project(db_session_factory, status="shot_review")

    # Create a shot with NO first frame set
    async with db_session_factory() as s:
        shot = Shot(
            project_id=pid,
            shot_id=1,
            text="Test shot",
            shot_type="Medium Shot",
            visual_description="Test visual",
            shot_duration=6,
            status="completed",
            align_with_previous=False,
            custom_first_frame_path=None,
        )
        s.add(shot)
        await s.commit()

    # Try to delete
    r = await client.delete(
        f"/api/projects/{pid}/shots/1/first-frame",
        headers=HEADERS,
    )

    # 5. Idempotency: 200 and stays None
    assert r.status_code == 200
    data = r.json()
    assert data["custom_first_frame_path"] is None

    shot = await _get_shot(db_session_factory, pid)
    assert shot.custom_first_frame_path is None
