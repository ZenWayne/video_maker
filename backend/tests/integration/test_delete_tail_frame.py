"""Integration tests for DELETE tail-frame endpoint.

target_last_frame_path now holds a COS key, not a local filesystem path
(Task 10). The file-removal assertions (clears DB/response, deletes the
stored object) moved to the real-COS test
tests/integration/test_uploads_oss.py::test_delete_tail_frame_removes_from_oss.
This file keeps only the storage-agnostic guard (409 while generating),
which doesn't touch object_store.
"""
from sqlalchemy import select

from tests.integration.conftest import HEADERS, _make_project
from app.models.project import Shot


async def _get_shot(db_session_factory, project_id, shot_id=1):
    async with db_session_factory() as s:
        result = await s.execute(
            select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
        )
        return result.scalar_one()


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

    # Guard: 409 when generating, path NOT cleared
    assert r.status_code == 409
    shot = await _get_shot(db_session_factory, pid)
    assert shot.target_last_frame_path == str(tail_file)
