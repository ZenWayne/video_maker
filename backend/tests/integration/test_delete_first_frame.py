"""Integration tests for DELETE first-frame endpoint (Task 6).

custom_first_frame_path now holds a COS key, not a local filesystem path
(Task 10). The file-removal assertions (clears DB, unlinks the stored file,
clears ff_status) moved to the real-COS test
tests/integration/test_uploads_oss.py::test_delete_first_frame_removes_from_oss.
This file keeps only the storage-agnostic guards (404, idempotency), which
don't touch object_store.
"""
import pytest
from sqlalchemy import select

from tests.integration.conftest import HEADERS, _make_project
from app.models.project import Shot


async def _get_shot(db_session_factory, project_id, shot_id=1):
    async with db_session_factory() as s:
        result = await s.execute(
            select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
        )
        return result.scalar_one()


async def test_delete_first_frame_shot_not_found(client, db_session_factory):
    """DELETE returns 404 when the shot doesn't exist."""
    pid = await _make_project(db_session_factory, status="shot_review")

    r = await client.delete(
        f"/api/projects/{pid}/shots/999/first-frame",
        headers=HEADERS,
    )

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

    assert r.status_code == 200
    data = r.json()
    assert data["custom_first_frame_path"] is None

    shot = await _get_shot(db_session_factory, pid)
    assert shot.custom_first_frame_path is None
