from unittest.mock import ANY
"""Integration tests for first frame generation (generate-first-frame endpoint).

Mirrors test_tail_frame_pipeline.py. Worker-level routing for the auto
first_frame path is covered by test_run_image_candidate_oss.py.
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


# ── POST /projects/{id}/shots/{shot_id}/generate-first-frame ─────────────────


async def test_generate_first_frame_success(client, db_session_factory):
    """generate-first-frame 现在创建 auto 候选并入队 run_image_candidate."""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="pending")

    r = await client.post(
        f"/api/projects/{pid}/shots/1/generate-first-frame", headers=HEADERS
    )
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    cid = body["candidate_id"]

    client.arq.enqueue_job.assert_called_once_with(
        "run_image_candidate", pid, 1, cid, f"user:{USER}", reservation_id=ANY
    )
    from app.models.project import ImageCandidate
    async with db_session_factory() as s:
        cand = (await s.execute(
            select(ImageCandidate).where(ImageCandidate.id == cid)
        )).scalar_one()
        assert cand.slot == "first_frame" and cand.prompt_source == "auto"


async def test_generate_first_frame_shot_not_found(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    r = await client.post(
        f"/api/projects/{pid}/shots/99/generate-first-frame",
        headers=HEADERS,
    )
    assert r.status_code == 404


async def test_generate_first_frame_any_status_ok(client, db_session_factory):
    """候选生成不再要求状态机 transition，即便 project 处于 draft 也是 202."""
    pid = await _make_project(db_session_factory, status="draft")
    await _add_shot(db_session_factory, pid, 1, status="pending")
    r = await client.post(
        f"/api/projects/{pid}/shots/1/generate-first-frame",
        headers=HEADERS,
    )
    assert r.status_code == 202


# NOTE: worker-level routing/failure coverage for the auto first_frame path
# (generate_first_frame call kwargs, done/failed candidate status) now lives in
# tests/integration/test_run_image_candidate.py
# (test_auto_first_frame_routes_to_generate_first_frame,
# test_failure_marks_candidate_failed_only) — run_first_frame_pipeline no
# longer exists, so the old worker-level tests here were removed rather than
# converted, to avoid duplicating that coverage.


# ── DELETE first-frame vs ff_status ──────────────────────────────────────────


async def test_delete_first_frame_blocked_while_generating(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    async with db_session_factory() as s:
        s.add(Shot(
            project_id=pid,
            shot_id=1,
            text="Hello",
            shot_type="Medium Shot",
            visual_description="Test",
            shot_duration=6,
            status="pending",
            align_with_previous=False,
            ff_status="generating",
        ))
        await s.commit()

    r = await client.request(
        "DELETE", f"/api/projects/{pid}/shots/1/first-frame", headers=HEADERS
    )
    assert r.status_code == 409


# test_delete_first_frame_clears_ff_status moved to the real-COS test
# tests/integration/test_uploads_oss.py::test_delete_first_frame_removes_from_oss
# — custom_first_frame_path now holds a COS key (Task 10); deleting it goes
# through object_store.delete(), which needs real COS credentials to test.


# ── GET /projects/{id} — first frame fields in response ──────────────────────


async def test_project_response_includes_first_frame_fields(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    async with db_session_factory() as s:
        s.add(Shot(
            project_id=pid,
            shot_id=1,
            text="Hello",
            shot_type="Medium Shot",
            visual_description="Test",
            shot_duration=6,
            status="completed",
            align_with_previous=False,
            ff_status="done",
        ))
        await s.commit()

    r = await client.get(f"/api/projects/{pid}")
    assert r.status_code == 200

    shot_data = r.json()["shots"][0]
    assert shot_data["ff_status"] == "done"
    assert "ff_error_message" in shot_data
