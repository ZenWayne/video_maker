"""Integration tests: regenerate must not resurrect tail frame; routing must not auto-generate.

Task 3 requirements:
- Part A: regenerate_shots must leave target_last_frame_path untouched (path-as-truth)
- Part B: _enqueue_next_shot_task must always enqueue run_shot_pipeline (no auto tail-frame routing)
- Regression: explicit generate-tail-frame endpoint still enqueues run_tail_frame_pipeline
"""
import pytest
from sqlalchemy import select

from tests.integration.conftest import (
    HEADERS,
    USER,
    _make_project,
    _add_shot,
)
from app.models.project import Shot


async def test_regenerate_does_not_resurrect_none_tail_frame(client, db_session_factory):
    """Shot with target_last_frame_path=None stays None after regenerate (path-as-truth)."""
    pid = await _make_project(db_session_factory, status="shot_review")
    async with db_session_factory() as s:
        s.add(Shot(
            project_id=pid,
            shot_id=1,
            text="Shot 1",
            shot_type="Medium Shot",
            visual_description="Visual",
            shot_duration=6,
            status="completed",
            align_with_previous=False,
            target_last_frame_path=None,
            tf_confirmed=False,
        ))
        await s.commit()

    r = await client.post(
        f"/api/projects/{pid}/regenerate-shots",
        json={"shot_ids": [1]},
        headers=HEADERS,
    )
    assert r.status_code == 202

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        # Path-as-truth: None stays None, tf_confirmed stays False
        assert shot.target_last_frame_path is None
        assert shot.tf_confirmed is False


async def test_regenerate_preserves_existing_tail_frame_path(client, db_session_factory):
    """Shot with target_last_frame_path=<path> keeps path unchanged after regenerate.

    OLD bug: if file was missing on disk, the path was CLEARED to None.
    NEW behavior: path is left as stored regardless of file existence on disk.
    """
    stored_path = "/fake/stored/tail.png"  # file does not exist in tmp_path
    pid = await _make_project(db_session_factory, status="shot_review")
    async with db_session_factory() as s:
        s.add(Shot(
            project_id=pid,
            shot_id=1,
            text="Shot 1",
            shot_type="Medium Shot",
            visual_description="Visual",
            shot_duration=6,
            status="completed",
            align_with_previous=False,
            target_last_frame_path=stored_path,
            tf_confirmed=True,
        ))
        await s.commit()

    r = await client.post(
        f"/api/projects/{pid}/regenerate-shots",
        json={"shot_ids": [1]},
        headers=HEADERS,
    )
    assert r.status_code == 202

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        # Path-as-truth: path preserved even if file doesn't exist on disk
        assert shot.target_last_frame_path == stored_path
        # tf_confirmed is left as stored (not cleared)
        assert shot.tf_confirmed is True


async def test_regenerate_connected_shot_no_target_enqueues_shot_pipeline(
    client, db_session_factory
):
    """Regenerating a connected shot with no target always enqueues run_shot_pipeline.

    OLD behavior: routed via _shot_needs_tail_frame.
    NEW behavior: always enqueue run_shot_pipeline directly (path-as-truth).
    """
    pid = await _make_project(db_session_factory, status="shot_review")
    async with db_session_factory() as s:
        s.add(Shot(
            project_id=pid,
            shot_id=1,
            text="Shot 1",
            shot_type="Medium Shot",
            visual_description="Visual",
            shot_duration=6,
            status="completed",
            align_with_previous=True,
            target_last_frame_path=None,
        ))
        await s.commit()

    client.arq.enqueue_job.reset_mock()

    r = await client.post(
        f"/api/projects/{pid}/regenerate-shots",
        json={"shot_ids": [1]},
        headers=HEADERS,
    )
    assert r.status_code == 202

    # Must enqueue video pipeline, NOT tail-frame pipeline
    client.arq.enqueue_job.assert_called_once_with(
        "run_shot_pipeline", pid, f"user:{USER}"
    )

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        # Path-as-truth: no tail frame stored, so none will be used
        assert shot.target_last_frame_path is None


async def test_explicit_generate_tail_frame_endpoint_still_works(client, db_session_factory):
    """Regression: the explicit generate-tail-frame endpoint still enqueues run_tail_frame_pipeline.

    This endpoint is out of scope for Task 3 changes and must remain working.
    """
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, shot_id=1, status="pending")

    client.arq.enqueue_job.reset_mock()

    r = await client.post(
        f"/api/projects/{pid}/shots/1/generate-tail-frame",
        headers=HEADERS,
    )
    assert r.status_code == 202
    client.arq.enqueue_job.assert_called_once_with(
        "run_tail_frame_pipeline", pid, 1, f"user:{USER}"
    )
