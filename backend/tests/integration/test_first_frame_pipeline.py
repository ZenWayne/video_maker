"""Integration tests for first frame generation (generate-first-frame + worker).

Mirrors test_tail_frame_pipeline.py. The billed model call
(image_generation.generate_first_frame) is always mocked.
"""
import json
import pytest
from unittest.mock import AsyncMock

from sqlalchemy import select

from tests.integration.conftest import (
    HEADERS,
    USER,
    _make_project,
    _add_shot,
)
from app.config import settings
from app.models.project import Project, Shot, ProjectStatus, ShotStatus
import worker.tasks as tasks
import app.services.image_generation as ff_generator


# ── POST /projects/{id}/shots/{shot_id}/generate-first-frame ─────────────────


async def test_generate_first_frame_success(client, db_session_factory):
    """Generate first frame enqueues run_first_frame_pipeline."""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="pending")

    r = await client.post(
        f"/api/projects/{pid}/shots/1/generate-first-frame",
        headers=HEADERS,
    )
    assert r.status_code == 202
    assert r.json()["status"] == "queued"

    client.arq.enqueue_job.assert_called_once_with(
        "run_first_frame_pipeline", pid, 1, f"user:{USER}"
    )

    async with db_session_factory() as s:
        result = await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )
        shot = result.scalar_one()
        assert shot.ff_status == "generating"
        assert shot.ff_error_message is None


async def test_generate_first_frame_shot_not_found(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    r = await client.post(
        f"/api/projects/{pid}/shots/99/generate-first-frame",
        headers=HEADERS,
    )
    assert r.status_code == 404


async def test_generate_first_frame_wrong_status(client, db_session_factory):
    """Cannot generate first frame when project is in draft."""
    pid = await _make_project(db_session_factory, status="draft")
    await _add_shot(db_session_factory, pid, 1, status="pending")
    r = await client.post(
        f"/api/projects/{pid}/shots/1/generate-first-frame",
        headers=HEADERS,
    )
    assert r.status_code == 409


# ── worker: run_first_frame_pipeline ──────────────────────────────────────────


PROJECT_ID = "proj-ff-pipeline"


async def _seed_ff_project(
    db_session_factory,
    tmp_path,
    *,
    custom_first_frame: str | None = None,
    custom_reference_paths: str | None = None,
):
    async with db_session_factory() as s:
        s.add(Project(
            id=PROJECT_ID,
            title="t",
            theme_text="theme",
            creator_name="tester",
            status=ProjectStatus.SHOT_GENERATING.value,
            aspect_ratio="9:16",
        ))
        s.add(Shot(
            project_id=PROJECT_ID,
            shot_id=1,
            text="dialogue",
            shot_type="Medium Shot",
            visual_description="a cozy study room",
            shot_duration=6,
            status=ShotStatus.PENDING.value,
            align_with_previous=False,
            use_prev_last_frame=False,
            auto_trim=False,
            ff_status="generating",
            custom_first_frame_path=custom_first_frame,
            custom_reference_paths=custom_reference_paths,
        ))
        await s.commit()


def _mk_img(tmp_path, name: str) -> str:
    p = tmp_path / name
    p.write_bytes(b"img-bytes-" + name.encode())
    return str(p)


def _mock_ff_generator(monkeypatch):
    """Mock the billed Gemini call; write a fake image to output_path."""
    async def _fake_generate(**kwargs):
        from pathlib import Path
        out = kwargs["output_path"]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"generated-first-frame")
        return out

    mock = AsyncMock(side_effect=_fake_generate)
    monkeypatch.setattr(ff_generator, "generate_first_frame", mock)
    return mock


@pytest.mark.asyncio
async def test_run_first_frame_pipeline_success(
    db_session_factory, redis, tmp_path, monkeypatch
):
    """Worker writes a ts_uuid file under custom_frames/ and updates the shot."""
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    old_frame = _mk_img(tmp_path, "old_first_frame.png")
    await _seed_ff_project(db_session_factory, tmp_path, custom_first_frame=old_frame)
    mock = _mock_ff_generator(monkeypatch)

    ctx = {"session_factory": db_session_factory, "redis": redis}
    await tasks.run_first_frame_pipeline(ctx, PROJECT_ID, 1, "user:tester")

    assert mock.called, "generate_first_frame was never called"
    _, kwargs = mock.call_args
    # The previous first frame is passed as scene context
    assert kwargs["context_frame_path"] == old_frame
    assert kwargs["visual_description"] == "a cozy study room"

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == PROJECT_ID, Shot.shot_id == 1)
        )).scalar_one()
        assert shot.ff_status == "done"
        assert shot.ff_error_message is None
        # Output lands in custom_frames/ (user-override territory) with ts_uuid name
        assert "custom_frames" in shot.custom_first_frame_path
        assert shot.custom_first_frame_path != old_frame
        from pathlib import Path
        assert Path(shot.custom_first_frame_path).read_bytes() == b"generated-first-frame"

        project = (await s.execute(
            select(Project).where(Project.id == PROJECT_ID)
        )).scalar_one()
        assert project.status == ProjectStatus.SHOT_REVIEW.value


@pytest.mark.asyncio
async def test_run_first_frame_pipeline_passes_object_refs(
    db_session_factory, redis, tmp_path, monkeypatch
):
    """Shot-level 参考物 (custom_reference_paths) are forwarded to the generator."""
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    frame = _mk_img(tmp_path, "first.png")
    prop1 = _mk_img(tmp_path, "prop1.png")
    prop2 = _mk_img(tmp_path, "prop2.png")
    await _seed_ff_project(
        db_session_factory, tmp_path,
        custom_first_frame=frame,
        custom_reference_paths=json.dumps([prop1, prop2]),
    )
    mock = _mock_ff_generator(monkeypatch)

    ctx = {"session_factory": db_session_factory, "redis": redis}
    await tasks.run_first_frame_pipeline(ctx, PROJECT_ID, 1, "user:tester")

    _, kwargs = mock.call_args
    assert kwargs["object_ref_paths"] == [prop1, prop2]


@pytest.mark.asyncio
async def test_run_first_frame_pipeline_failure(
    db_session_factory, redis, tmp_path, monkeypatch
):
    """Generator failure sets ff_status=failed + error message, back to SHOT_REVIEW."""
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    frame = _mk_img(tmp_path, "first.png")
    await _seed_ff_project(db_session_factory, tmp_path, custom_first_frame=frame)
    mock = AsyncMock(side_effect=RuntimeError("model blew up"))
    monkeypatch.setattr(ff_generator, "generate_first_frame", mock)

    ctx = {"session_factory": db_session_factory, "redis": redis}
    await tasks.run_first_frame_pipeline(ctx, PROJECT_ID, 1, "user:tester")

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == PROJECT_ID, Shot.shot_id == 1)
        )).scalar_one()
        assert shot.ff_status == "failed"
        assert "model blew up" in shot.ff_error_message
        # The stored first frame is untouched on failure
        assert shot.custom_first_frame_path == frame

        project = (await s.execute(
            select(Project).where(Project.id == PROJECT_ID)
        )).scalar_one()
        assert project.status == ProjectStatus.SHOT_REVIEW.value


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


async def test_delete_first_frame_clears_ff_status(client, db_session_factory, tmp_path):
    pid = await _make_project(db_session_factory, status="shot_review")
    frame = tmp_path / "ff.png"
    frame.write_bytes(b"\x89PNG")
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
            ff_status="failed",
            ff_error_message="boom",
            custom_first_frame_path=str(frame),
        ))
        await s.commit()

    r = await client.request(
        "DELETE", f"/api/projects/{pid}/shots/1/first-frame", headers=HEADERS
    )
    assert r.status_code == 200

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        assert shot.custom_first_frame_path is None
        assert shot.ff_status is None
        assert shot.ff_error_message is None
    assert not frame.exists()


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
