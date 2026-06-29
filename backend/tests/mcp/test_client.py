import pytest
from tests.mcp.conftest import seed_project


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


async def test_replace_storyboard(backend, db_session_factory):
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
