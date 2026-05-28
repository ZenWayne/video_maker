"""Integration tests for tail frame pipeline endpoints."""
import json
import pytest

from tests.integration.conftest import (
    HEADERS,
    USER,
    _make_project,
    _add_shots,
    _add_shot,
    _add_character_image,
)
from app.models.project import Shot
from sqlalchemy import select


# ── POST /projects/{id}/shots/{shot_id}/generate-tail-frame ──────────────────


async def test_generate_tail_frame_success(client, db_session_factory):
    """Generate tail frame enqueues run_tail_frame_pipeline."""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="pending")

    r = await client.post(
        f"/api/projects/{pid}/shots/1/generate-tail-frame",
        headers=HEADERS,
    )
    assert r.status_code == 202
    assert r.json()["status"] == "queued"

    client.arq.enqueue_job.assert_called_once_with(
        "run_tail_frame_pipeline", pid, 1, f"user:{USER}"
    )

    # Shot tf_status should be "generating"
    async with db_session_factory() as s:
        result = await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )
        shot = result.scalar_one()
        assert shot.tf_status == "generating"
        assert shot.tf_confirmed is False


async def test_generate_tail_frame_shot_not_found(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    r = await client.post(
        f"/api/projects/{pid}/shots/99/generate-tail-frame",
        headers=HEADERS,
    )
    assert r.status_code == 404


async def test_generate_tail_frame_wrong_status(client, db_session_factory):
    """Cannot generate tail frame when project is in draft."""
    pid = await _make_project(db_session_factory, status="draft")
    await _add_shot(db_session_factory, pid, 1, status="pending")
    r = await client.post(
        f"/api/projects/{pid}/shots/1/generate-tail-frame",
        headers=HEADERS,
    )
    assert r.status_code == 409


# ── POST /projects/{id}/shots/{shot_id}/confirm-tail-frame ───────────────────


async def test_confirm_tail_frame_success(client, db_session_factory, tmp_path):
    """Confirm tail frame sets tf_confirmed=True and enqueues video generation."""
    pid = await _make_project(db_session_factory, status="shot_review")
    async with db_session_factory() as s:
        shot = Shot(
            project_id=pid,
            shot_id=1,
            text="Hello",
            shot_type="Medium Shot",
            visual_description="Test",
            shot_duration=6,
            status="pending",
            align_with_previous=False,
            tf_status="done",
            target_last_frame_path=str(tmp_path / "target.png"),
        )
        s.add(shot)
        await s.commit()

    # Create the fake target file
    (tmp_path / "target.png").write_bytes(b"\x89PNG")

    r = await client.post(
        f"/api/projects/{pid}/shots/1/confirm-tail-frame",
        headers=HEADERS,
    )
    assert r.status_code == 202
    assert r.json()["tf_confirmed"] is True

    # Verify DB
    async with db_session_factory() as s:
        result = await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )
        shot = result.scalar_one()
        assert shot.tf_confirmed is True

    # Verify video generation was enqueued
    client.arq.enqueue_job.assert_called_with(
        "run_shot_pipeline", pid, f"user:{USER}"
    )


async def test_confirm_tail_frame_not_generated(client, db_session_factory):
    """Cannot confirm when tf_status is not 'done'."""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="pending")

    r = await client.post(
        f"/api/projects/{pid}/shots/1/confirm-tail-frame",
        headers=HEADERS,
    )
    assert r.status_code == 400


async def test_confirm_tail_frame_no_file(client, db_session_factory):
    """Cannot confirm when target_last_frame_path is None."""
    pid = await _make_project(db_session_factory, status="shot_review")
    async with db_session_factory() as s:
        shot = Shot(
            project_id=pid,
            shot_id=1,
            text="Hello",
            shot_type="Medium Shot",
            visual_description="Test",
            shot_duration=6,
            status="pending",
            align_with_previous=False,
            tf_status="done",
            target_last_frame_path=None,
        )
        s.add(shot)
        await s.commit()

    r = await client.post(
        f"/api/projects/{pid}/shots/1/confirm-tail-frame",
        headers=HEADERS,
    )
    assert r.status_code == 400


# ── GET /projects/{id} — tail frame fields in response ───────────────────────


async def test_project_response_includes_tail_frame_fields(client, db_session_factory):
    """Project detail response includes tf_status, tf_confirmed, target_last_frame_path."""
    pid = await _make_project(db_session_factory, status="shot_review")
    async with db_session_factory() as s:
        shot = Shot(
            project_id=pid,
            shot_id=1,
            text="Hello",
            shot_type="Medium Shot",
            visual_description="Test",
            shot_duration=6,
            status="completed",
            align_with_previous=False,
            tf_status="done",
            tf_confirmed=True,
            target_last_frame_path="/fake/target.png",
        )
        s.add(shot)
        await s.commit()

    r = await client.get(f"/api/projects/{pid}")
    assert r.status_code == 200

    shot_data = r.json()["shots"][0]
    assert shot_data["tf_status"] == "done"
    assert shot_data["tf_confirmed"] is True
    assert "target_last_frame_path" in shot_data
