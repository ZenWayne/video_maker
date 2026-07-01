"""Integration tests for pipeline workflow endpoints."""
import pytest
from unittest.mock import AsyncMock, patch

from tests.integration.conftest import (
    HEADERS, USER,
    _make_project, _add_shots, _add_shot, _add_character_image,
)


# ── POST /projects/{id}/start ──────────────────────────────────────────────────

async def test_start_success(client, project_in_draft_with_image):
    pid = project_in_draft_with_image["project"]["id"]
    r = await client.post(f"/api/projects/{pid}/start", headers=HEADERS)
    assert r.status_code == 202

    p = (await client.get(f"/api/projects/{pid}")).json()
    assert p["status"] == "scripting"
    client.arq.enqueue_job.assert_called_once_with(
        "run_screenwriter", pid, f"user:{USER}"
    )


async def test_start_no_character_image(client, project_in_draft):
    pid = project_in_draft["id"]
    r = await client.post(f"/api/projects/{pid}/start", headers=HEADERS)
    assert r.status_code == 400


async def test_start_invalid_transition(client, db_session_factory):
    # SCRIPTING → SCRIPTING is not allowed
    pid = await _make_project(db_session_factory, status="scripting")
    await _add_character_image(db_session_factory, pid)
    r = await client.post(f"/api/projects/{pid}/start", headers=HEADERS)
    assert r.status_code == 409


async def test_start_no_user_header(client, project_in_draft_with_image):
    pid = project_in_draft_with_image["project"]["id"]
    r = await client.post(f"/api/projects/{pid}/start")
    assert r.status_code == 400


# ── PATCH /projects/{id}/storyboard ───────────────────────────────────────────

async def test_patch_storyboard_scene_overview(client, project_in_script_review):
    pid = project_in_script_review
    r = await client.patch(
        f"/api/projects/{pid}/storyboard",
        json={"scene_overview": "Updated scene overview"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["scene_overview"] == "Updated scene overview"


async def test_patch_storyboard_shots(client, project_in_script_review):
    pid = project_in_script_review
    r = await client.patch(
        f"/api/projects/{pid}/storyboard",
        json={
            "shots": [{
                "shot_id": 1,
                "text": "New dialogue text",
                "shot_type": "Close-up",
                "visual_description": "Updated visual description",
                "shot_duration": 4,
                "align_with_previous": True,
            }]
        },
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "script_review"


async def test_patch_storyboard_wrong_status(client, project_in_shot_review):
    pid = project_in_shot_review
    r = await client.patch(
        f"/api/projects/{pid}/storyboard",
        json={"scene_overview": "test"},
        headers=HEADERS,
    )
    assert r.status_code == 409


async def test_patch_storyboard_not_found(client):
    r = await client.patch(
        "/api/projects/nonexistent/storyboard",
        json={"scene_overview": "test"},
        headers=HEADERS,
    )
    assert r.status_code == 404


# ── POST /projects/{id}/approve-script ────────────────────────────────────────

async def test_approve_script_success(client, project_in_script_review):
    pid = project_in_script_review
    r = await client.post(f"/api/projects/{pid}/approve-script", headers=HEADERS)
    assert r.status_code == 202

    p = (await client.get(f"/api/projects/{pid}")).json()
    assert p["status"] == "shot_generating"
    # Path-as-truth: _enqueue_next_shot_task always enqueues run_shot_pipeline.
    # Tail frame use is decided by the worker (resolve_tail_frame), not here.
    client.arq.enqueue_job.assert_called_with(
        "run_shot_pipeline", pid, f"user:{USER}"
    )


async def test_approve_script_invalid_transition(client, db_session_factory):
    # SCRIPTING cannot transition to SHOT_GENERATING via approve-script
    pid = await _make_project(db_session_factory, status="scripting")
    r = await client.post(f"/api/projects/{pid}/approve-script", headers=HEADERS)
    assert r.status_code == 409


# ── POST /projects/{id}/regenerate-script ─────────────────────────────────────

async def test_regenerate_script_success(client, project_in_script_review):
    pid = project_in_script_review
    r = await client.post(f"/api/projects/{pid}/regenerate-script", headers=HEADERS)
    assert r.status_code == 202

    p = (await client.get(f"/api/projects/{pid}")).json()
    assert p["status"] == "scripting"
    assert p["shots"] == []
    client.arq.enqueue_job.assert_called_once_with(
        "run_screenwriter", pid, f"user:{USER}"
    )


async def test_regenerate_script_invalid_transition(client, db_session_factory):
    # SHOT_GENERATING cannot transition to SCRIPTING
    pid = await _make_project(db_session_factory, status="shot_generating")
    r = await client.post(f"/api/projects/{pid}/regenerate-script", headers=HEADERS)
    assert r.status_code == 409


# ── POST /projects/{id}/regenerate-shots ──────────────────────────────────────

async def test_regenerate_shots_success(client, project_in_shot_review):
    pid = project_in_shot_review
    r = await client.post(
        f"/api/projects/{pid}/regenerate-shots",
        json={"shot_ids": [1, 2]},
        headers=HEADERS,
    )
    assert r.status_code == 202

    p = (await client.get(f"/api/projects/{pid}")).json()
    assert p["status"] == "shot_generating"
    shots_by_id = {s["shot_id"]: s for s in p["shots"]}
    assert shots_by_id[1]["status"] == "pending"
    assert shots_by_id[2]["status"] == "pending"
    assert shots_by_id[3]["status"] == "completed"  # not in regenerate list
    client.arq.enqueue_job.assert_called_once()


async def test_regenerate_shots_preserves_director_inputs(client, db_session_factory):
    """Regenerate must KEEP the cached motion_prompt so the re-run reuses the
    existing director take instead of regenerating it. The first frame is not
    stored — it is re-resolved at gen time. Path-as-truth: target_last_frame_path
    is left EXACTLY as stored regardless of whether the file exists on disk."""
    from sqlalchemy import select
    from app.models.project import Shot

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shots(db_session_factory, pid, count=1, status="completed")

    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        shot.motion_prompt = "old camera pan"
        shot.target_last_frame_path = "/tmp/does/not/exist/tail.png"  # missing on disk
        shot.tf_confirmed = True
        s.add(shot)
        await s.commit()

    r = await client.post(
        f"/api/projects/{pid}/regenerate-shots",
        json={"shot_ids": [1]},
        headers=HEADERS,
    )
    assert r.status_code == 202

    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        assert shot.motion_prompt == "old camera pan"            # reused, not regenerated
        # Path-as-truth: path left as stored even if file is missing on disk
        assert shot.target_last_frame_path == "/tmp/does/not/exist/tail.png"
        assert shot.tf_confirmed is True


async def test_regenerate_shots_skips_tail_frame_generation(
    client, db_session_factory, project_in_shot_review
):
    """生成分镜 (regenerate) always goes directly to video generation.
    Path-as-truth: _enqueue_next_shot_task always enqueues run_shot_pipeline;
    the tail frame is decided by target_last_frame_path presence in the worker."""
    from sqlalchemy import select
    from app.models.project import Shot

    pid = project_in_shot_review  # shot 1 disconnected, no tail frame yet
    client.arq.enqueue_job.reset_mock()

    r = await client.post(
        f"/api/projects/{pid}/regenerate-shots",
        json={"shot_ids": [1]},
        headers=HEADERS,
    )
    assert r.status_code == 202

    # Straight to video generation, NOT tail-frame generation
    client.arq.enqueue_job.assert_called_once_with(
        "run_shot_pipeline", pid, f"user:{USER}"
    )
    async with db_session_factory() as s:
        shot = (
            await s.execute(
                select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
            )
        ).scalar_one()
        # Path-as-truth: no tail frame stored, so none will be used
        assert shot.target_last_frame_path is None


async def test_regenerate_shots_invalid_transition(client, db_session_factory):
    # SCRIPTING cannot transition to SHOT_GENERATING
    pid = await _make_project(db_session_factory, status="scripting")
    r = await client.post(
        f"/api/projects/{pid}/regenerate-shots",
        json={"shot_ids": [1]},
        headers=HEADERS,
    )
    assert r.status_code == 409


async def test_regenerate_shots_no_user_header(client, project_in_shot_review):
    pid = project_in_shot_review
    r = await client.post(
        f"/api/projects/{pid}/regenerate-shots",
        json={"shot_ids": [1]},
    )
    assert r.status_code == 400


# ── POST /projects/{id}/continue-generation ──────────────────────────────────

async def test_continue_generation_success(client, db_session_factory):
    # shot_review with some pending shots → 202
    # Path-as-truth: continue-generation always enqueues run_shot_pipeline (no auto tail-frame).
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, shot_id=1, status="completed")
    await _add_shot(db_session_factory, pid, shot_id=2, status="pending")
    r = await client.post(f"/api/projects/{pid}/continue-generation", headers=HEADERS)
    assert r.status_code == 202

    p = (await client.get(f"/api/projects/{pid}")).json()
    assert p["status"] == "shot_generating"
    client.arq.enqueue_job.assert_called_with(
        "run_shot_pipeline", pid, f"user:{USER}"
    )


async def test_continue_generation_no_pending(client, project_in_shot_review):
    # shot_review with all completed → 400
    pid = project_in_shot_review
    r = await client.post(f"/api/projects/{pid}/continue-generation", headers=HEADERS)
    assert r.status_code == 400


async def test_continue_generation_wrong_state(client, db_session_factory):
    # draft → 409
    pid = await _make_project(db_session_factory, status="draft")
    r = await client.post(f"/api/projects/{pid}/continue-generation", headers=HEADERS)
    assert r.status_code == 409


# ── PATCH /projects/{id}/shots/{shot_id} ──────────────────────────────────────

async def test_patch_shot_motion_prompt(client, project_in_shot_review):
    pid = project_in_shot_review
    r = await client.patch(
        f"/api/projects/{pid}/shots/1",
        json={"motion_prompt": "Camera pans slowly left"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["motion_prompt"] == "Camera pans slowly left"
    assert data["shot_id"] == 1


async def test_patch_shot_align_with_previous(client, project_in_shot_review):
    pid = project_in_shot_review
    r = await client.patch(
        f"/api/projects/{pid}/shots/2",
        json={"align_with_previous": False},
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["align_with_previous"] is False


async def test_patch_shot_not_found(client, project_in_shot_review):
    pid = project_in_shot_review
    r = await client.patch(
        f"/api/projects/{pid}/shots/999",
        json={"motion_prompt": "test"},
        headers=HEADERS,
    )
    assert r.status_code == 404


# ── POST /projects/{id}/shots/{shot_id}/ai-edit ───────────────────────────────

async def test_ai_edit_shot_success(client, project_in_shot_review):
    pid = project_in_shot_review
    mock_result = {"text": "Revised text", "visual_description": "Revised visual"}

    with patch("app.agents.shot_editor.run_shot_editor", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        r = await client.post(
            f"/api/projects/{pid}/shots/1/ai-edit",
            json={"instruction": "Make it more dramatic"},
        )

    assert r.status_code == 200
    assert r.json() == mock_result


async def test_ai_edit_shot_project_not_found(client):
    with patch("app.agents.shot_editor.run_shot_editor", new_callable=AsyncMock):
        r = await client.post(
            "/api/projects/nonexistent/shots/1/ai-edit",
            json={"instruction": "test"},
        )
    assert r.status_code == 404


async def test_ai_edit_shot_not_found(client, project_in_shot_review):
    pid = project_in_shot_review
    with patch("app.agents.shot_editor.run_shot_editor", new_callable=AsyncMock):
        r = await client.post(
            f"/api/projects/{pid}/shots/999/ai-edit",
            json={"instruction": "test"},
        )
    assert r.status_code == 404


# ── POST /projects/{id}/export ────────────────────────────────────────────────

async def test_export_success(client, project_in_shot_review):
    pid = project_in_shot_review
    r = await client.post(f"/api/projects/{pid}/export", headers=HEADERS)
    assert r.status_code == 202

    p = (await client.get(f"/api/projects/{pid}")).json()
    assert p["status"] == "exporting"
    client.arq.enqueue_job.assert_called_once_with(
        "run_merger", pid, f"user:{USER}"
    )


async def test_export_shots_not_completed(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, shot_id=1, status="completed")
    await _add_shot(db_session_factory, pid, shot_id=2, status="pending")
    r = await client.post(f"/api/projects/{pid}/export", headers=HEADERS)
    assert r.status_code == 400


async def test_export_invalid_transition(client, db_session_factory):
    # SCRIPTING cannot transition to EXPORTING; use completed shot to bypass shots check
    pid = await _make_project(db_session_factory, status="scripting")
    await _add_shot(db_session_factory, pid, shot_id=1, status="completed")
    r = await client.post(f"/api/projects/{pid}/export", headers=HEADERS)
    assert r.status_code == 409


# ── POST /projects/{id}/reset-to-script ───────────────────────────────────────

async def test_reset_to_script_success(client, project_in_shot_review):
    pid = project_in_shot_review
    r = await client.post(f"/api/projects/{pid}/reset-to-script", headers=HEADERS)
    assert r.status_code == 202

    p = (await client.get(f"/api/projects/{pid}")).json()
    assert p["status"] == "script_review"


async def test_reset_to_script_invalid_transition(client, db_session_factory):
    # DRAFT cannot transition to SCRIPT_REVIEW
    pid = await _make_project(db_session_factory, status="draft")
    r = await client.post(f"/api/projects/{pid}/reset-to-script", headers=HEADERS)
    assert r.status_code == 409


# ── POST /projects/{id}/reset ─────────────────────────────────────────────────

async def test_reset_project_success(client, db_session_factory):
    # Only FAILED can transition to DRAFT
    pid = await _make_project(db_session_factory, status="failed")
    await _add_shots(db_session_factory, pid, count=2, status="pending")
    r = await client.post(f"/api/projects/{pid}/reset", headers=HEADERS)
    assert r.status_code == 200

    p = (await client.get(f"/api/projects/{pid}")).json()
    assert p["status"] == "draft"
    assert p["shots"] == []


async def test_reset_project_invalid_transition(client, db_session_factory):
    # SCRIPTING cannot transition to DRAFT
    pid = await _make_project(db_session_factory, status="scripting")
    r = await client.post(f"/api/projects/{pid}/reset", headers=HEADERS)
    assert r.status_code == 409


# ── GET /projects/{id}/stream ─────────────────────────────────────────────────

async def test_stream_project_not_found(client):
    r = await client.get("/api/projects/nonexistent/stream")
    assert r.status_code == 404


async def test_stream_snapshot_uses_media_urls(client, db_session_factory, redis):
    """state_snapshot must return /api/media/... URLs, not raw filesystem paths."""
    import json
    from app.config import settings
    from app.models.project import Shot
    from app.api.stream import event_generator

    pid = await _make_project(db_session_factory, status="shot_review")

    # Insert a shot with absolute filesystem paths (as the worker would)
    storage = settings.storage_root
    async with db_session_factory() as s:
        s.add(Shot(
            project_id=pid,
            shot_id=1,
            text="dialogue",
            shot_type="Medium Shot",
            visual_description="visual",
            shot_duration=6,
            status="completed",
            video_path=f"{storage}/projects/{pid}/shots/shot_1/output.mp4",
            last_frame_path=f"{storage}/projects/{pid}/shots/shot_1/last_frame.png",
        ))
        await s.commit()

    # Read the first yielded event (state_snapshot) from the generator directly
    gen = event_generator(redis, pid)
    first_event_json = await gen.__anext__()
    await gen.aclose()

    event = json.loads(first_event_json)
    assert event["type"] == "state_snapshot"
    shot = event["data"]["shots"][0]
    assert shot["video_path"].startswith("/api/media/"), \
        f"video_path should be a media URL, got: {shot['video_path']}"
    assert shot["last_frame_path"].startswith("/api/media/"), \
        f"last_frame_path should be a media URL, got: {shot['last_frame_path']}"


# ── GET /projects/{id}/final.mp4 ─────────────────────────────────────────────

async def test_download_final_not_ready(client, project_in_draft):
    pid = project_in_draft["id"]
    r = await client.get(f"/api/projects/{pid}/final.mp4")
    assert r.status_code == 404


async def test_download_final_success(client, db_session_factory, tmp_path):
    from app.services.storage import final_video_path
    from app.config import settings

    pid = await _make_project(db_session_factory, status="exported")
    # Create the merged.mp4 file in the patched storage location
    video_path = final_video_path(pid)
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"fake-video-content")

    r = await client.get(f"/api/projects/{pid}/final.mp4")
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"


# ── POST /projects/{id}/shots/{shot_id}/delete-tail-frame ──────────────────────

async def _give_tail_frame(sf, pid, shot_id, *, tf_status="done", tf_confirmed=True):
    """Give a shot a generated tail frame backed by a real file on disk."""
    from sqlalchemy import select
    from app.models.project import Shot
    from app.services.storage import shot_target_last_frame_path

    path = shot_target_last_frame_path(pid, shot_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake-png")
    async with sf() as s:
        result = await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == shot_id)
        )
        shot = result.scalar_one()
        shot.tf_status = tf_status
        shot.tf_confirmed = tf_confirmed
        shot.target_last_frame_path = str(path)
        await s.commit()
    return path


async def test_delete_tail_frame_clears_state_without_generating_video(
    client, db_session_factory, project_in_shot_review
):
    pid = project_in_shot_review
    path = await _give_tail_frame(db_session_factory, pid, 1)
    client.arq.enqueue_job.reset_mock()

    r = await client.post(
        f"/api/projects/{pid}/shots/1/delete-tail-frame", headers=HEADERS
    )
    assert r.status_code in (200, 202)
    body = r.json()
    # New return shape: target_last_frame_path + tf_status
    assert body["target_last_frame_path"] is None
    assert body["tf_status"] is None

    # Must NOT auto-generate video
    client.arq.enqueue_job.assert_not_called()

    # Project stays in shot_review (not advanced to shot_generating)
    p = (await client.get(f"/api/projects/{pid}")).json()
    assert p["status"] == "shot_review"

    # Shot tail-frame state fully cleared
    shot = next(s for s in p["shots"] if s["shot_id"] == 1)
    assert shot["tf_status"] is None
    assert shot["tf_confirmed"] is False
    assert shot["target_last_frame_path"] is None

    # Physical tail-frame file removed
    assert not path.exists()


async def test_delete_tail_frame_rejected_while_generating(
    client, db_session_factory, project_in_shot_review
):
    pid = project_in_shot_review
    await _give_tail_frame(
        db_session_factory, pid, 1, tf_status="generating", tf_confirmed=False
    )
    r = await client.post(
        f"/api/projects/{pid}/shots/1/delete-tail-frame", headers=HEADERS
    )
    assert r.status_code == 409


async def test_delete_tail_frame_shot_not_found(client, project_in_shot_review):
    pid = project_in_shot_review
    r = await client.post(
        f"/api/projects/{pid}/shots/999/delete-tail-frame", headers=HEADERS
    )
    assert r.status_code == 404


# ── PUT /projects/{id}/storyboard (full replace) ──────────────────────────────

async def test_put_storyboard_upsert_and_add(client, db_session_factory, project_in_script_review):
    pid = project_in_script_review  # has shots 1,2,3
    r = await client.put(
        f"/api/projects/{pid}/storyboard",
        json={
            "scene_overview": "new overview",
            "shots": [
                {"shot_id": 1, "text": "edited one", "shot_type": "Close-up",
                 "visual_description": "v1", "shot_duration": 4, "align_with_previous": False},
                {"shot_id": 4, "text": "brand new", "shot_type": "Wide Shot",
                 "visual_description": "v4", "shot_duration": 8, "align_with_previous": True},
            ],
        },
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    from app.models.project import Shot
    from sqlalchemy import select
    async with db_session_factory() as s:
        rows = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalars().all()
    by_id = {row.shot_id: row for row in rows}
    assert set(by_id) == {1, 4}            # shots 2,3 deleted; 4 created
    assert by_id[1].text == "edited one"
    assert by_id[1].shot_type == "Close-up"
    assert by_id[4].text == "brand new"


async def test_put_storyboard_rewrites_json(client, db_session_factory, project_in_script_review, tmp_path):
    pid = project_in_script_review
    r = await client.put(
        f"/api/projects/{pid}/storyboard",
        json={"scene_overview": "ov", "shots": [
            {"shot_id": 1, "text": "only", "shot_type": "Medium Shot",
             "visual_description": "v", "shot_duration": 6, "align_with_previous": False},
        ]},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    import json
    from app.services.storage import storyboard_path
    data = json.loads(storyboard_path(pid).read_text(encoding="utf-8"))
    assert data["scene_overview"] == "ov"
    assert [s["shot_id"] for s in data["shots"]] == [1]
    assert data["shots"][0]["text"] == "only"


async def test_put_storyboard_wrong_status(client, project_in_shot_review):
    pid = project_in_shot_review
    r = await client.put(
        f"/api/projects/{pid}/storyboard",
        json={"scene_overview": "x", "shots": [
            {"shot_id": 1, "text": "t", "shot_type": "Medium Shot",
             "visual_description": "v", "shot_duration": 6, "align_with_previous": False}]},
        headers=HEADERS,
    )
    assert r.status_code == 409


async def test_put_storyboard_not_found(client):
    r = await client.put(
        "/api/projects/nonexistent/storyboard",
        json={"scene_overview": "x", "shots": [
            {"shot_id": 1, "text": "t", "shot_type": "Medium Shot",
             "visual_description": "v", "shot_duration": 6, "align_with_previous": False}]},
        headers=HEADERS,
    )
    assert r.status_code == 404


async def test_put_storyboard_duplicate_shot_id(client, project_in_script_review):
    pid = project_in_script_review
    r = await client.put(
        f"/api/projects/{pid}/storyboard",
        json={"scene_overview": "x", "shots": [
            {"shot_id": 1, "text": "a", "shot_type": "Medium Shot",
             "visual_description": "v", "shot_duration": 6, "align_with_previous": False},
            {"shot_id": 1, "text": "b", "shot_type": "Medium Shot",
             "visual_description": "v", "shot_duration": 6, "align_with_previous": False}]},
        headers=HEADERS,
    )
    assert r.status_code == 422


async def test_put_storyboard_empty_shots(client, project_in_script_review):
    pid = project_in_script_review
    r = await client.put(
        f"/api/projects/{pid}/storyboard",
        json={"scene_overview": "x", "shots": []},
        headers=HEADERS,
    )
    assert r.status_code == 422


async def test_put_storyboard_deletes_shot_output_dir(client, db_session_factory, project_in_script_review):
    pid = project_in_script_review  # shots 1,2,3
    from app.services.storage import shot_dir
    leftover = shot_dir(pid, 3)
    leftover.mkdir(parents=True, exist_ok=True)
    (leftover / "output.mp4").write_bytes(b"stale")
    assert leftover.exists()

    r = await client.put(
        f"/api/projects/{pid}/storyboard",
        json={"scene_overview": "ov", "shots": [
            {"shot_id": 1, "text": "keep", "shot_type": "Medium Shot",
             "visual_description": "v", "shot_duration": 6, "align_with_previous": False}]},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    assert not leftover.exists()  # shot 3 dir removed
