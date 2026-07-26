import pytest
from tests.mcp.conftest import seed_project, seed_reference_image
from tests.integration.conftest_cos import requires_cos


async def test_list_and_get_project(backend, db_session_factory):
    pid = await seed_project(db_session_factory)
    projects = await backend.list_projects()
    assert any(p["id"] == pid for p in projects)

    proj = await backend.get_project(pid)
    assert proj["id"] == pid
    assert len(proj["shots"]) == 3


async def test_patch_shot(backend, db_session_factory):
    pid = await seed_project(db_session_factory)
    out = await backend.patch_shot(pid, 1, {"text": "patched", "motion_prompt": "zoom in"})
    assert out["text"] == "patched"
    assert out["motion_prompt"] == "zoom in"


@requires_cos
async def test_replace_storyboard(backend, db_session_factory, cos_prefix):
    pid = await seed_project(db_session_factory)
    out = await backend.replace_storyboard(pid, "ov", [
        {"shot_id": 1, "text": "a", "shot_type": "Close-up",
         "visual_description": "v", "shot_duration": 4, "align_with_previous": False},
    ])
    assert out["scene_overview"] == "ov"


async def test_backend_error_on_404(backend):
    from mcp_server.client import BackendError
    with pytest.raises(BackendError) as ei:
        await backend.get_project("nope")
    assert ei.value.status_code == 404


async def test_client_start_project_requires_character_image(backend, db_session_factory):
    from mcp_server.client import BackendError
    pid = await seed_project(db_session_factory, status="draft")
    with pytest.raises(BackendError) as ei:
        await backend.start_project(pid)
    assert ei.value.status_code == 400


async def test_client_start_project_queues(backend, db_session_factory):
    pid = await seed_project(db_session_factory, status="draft")
    await seed_reference_image(db_session_factory, pid)
    res = await backend.start_project(pid)
    assert res["status"] == "queued"


async def test_client_approve_script(backend, db_session_factory):
    pid = await seed_project(db_session_factory, status="script_review")
    res = await backend.approve_script(pid)
    assert res["status"] == "queued"


async def test_client_regenerate_shots(backend, db_session_factory):
    pid = await seed_project(db_session_factory, status="shot_review")
    res = await backend.regenerate_shots(pid, [1, 2])
    assert res["status"] == "queued"


async def test_client_continue_generation(backend, db_session_factory):
    pid = await seed_project(db_session_factory, status="shot_review")
    res = await backend.continue_generation(pid)
    assert res["status"] == "queued"


async def test_client_cancel_generation(backend, db_session_factory):
    pid = await seed_project(db_session_factory, status="shot_generating")
    res = await backend.cancel_generation(pid)
    assert res["status"] == "shot_review"
