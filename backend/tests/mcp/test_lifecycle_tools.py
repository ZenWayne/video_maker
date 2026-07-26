import json
import pytest
from fastmcp import Client

from tests.mcp.conftest import seed_project, seed_reference_image


def _payload(result):
    if getattr(result, "data", None) is not None:
        return result.data
    return json.loads(result.content[0].text)


@pytest.fixture
def server(backend):
    from mcp_server.server import create_server
    return create_server(backend)


async def test_start_generation_queues(server, db_session_factory):
    pid = await seed_project(db_session_factory, status="draft")
    await seed_reference_image(db_session_factory, pid)
    async with Client(server) as c:
        res = await c.call_tool("start_generation", {"project_id": pid})
    assert _payload(res)["status"] == "queued"


async def test_start_generation_without_character_image_is_structured_error(
    server, db_session_factory
):
    pid = await seed_project(db_session_factory, status="draft")
    async with Client(server) as c:
        res = await c.call_tool("start_generation", {"project_id": pid})
    data = _payload(res)
    assert data["ok"] is False
    assert data["status_code"] == 400
    assert "character" in data["error"]


async def test_approve_script_queues(server, db_session_factory):
    pid = await seed_project(db_session_factory, status="script_review")
    async with Client(server) as c:
        res = await c.call_tool("approve_script", {"project_id": pid})
    assert _payload(res)["status"] == "queued"


async def test_approve_script_wrong_status_409(server, db_session_factory):
    pid = await seed_project(db_session_factory, status="draft")
    async with Client(server) as c:
        res = await c.call_tool("approve_script", {"project_id": pid})
    data = _payload(res)
    assert data["ok"] is False
    assert data["status_code"] == 409


async def test_regenerate_shots_queues(server, db_session_factory):
    pid = await seed_project(db_session_factory, status="shot_review")
    async with Client(server) as c:
        res = await c.call_tool("regenerate_shots", {"project_id": pid, "shot_ids": [1, 3]})
    assert _payload(res)["status"] == "queued"


async def test_regenerate_shots_rejects_empty_list(server, db_session_factory):
    pid = await seed_project(db_session_factory, status="shot_review")
    async with Client(server) as c:
        with pytest.raises(Exception, match="shot_ids"):
            await c.call_tool("regenerate_shots", {"project_id": pid, "shot_ids": []})


async def test_regenerate_shots_rejects_non_positive_ids(server, db_session_factory):
    pid = await seed_project(db_session_factory, status="shot_review")
    async with Client(server) as c:
        with pytest.raises(Exception, match="positive"):
            await c.call_tool("regenerate_shots", {"project_id": pid, "shot_ids": [1, 0]})


async def test_continue_generation_queues(server, db_session_factory):
    pid = await seed_project(db_session_factory, status="shot_review")
    async with Client(server) as c:
        res = await c.call_tool("continue_generation", {"project_id": pid})
    assert _payload(res)["status"] == "queued"


async def test_continue_generation_wrong_status_409(server, db_session_factory):
    pid = await seed_project(db_session_factory, status="draft")
    async with Client(server) as c:
        res = await c.call_tool("continue_generation", {"project_id": pid})
    data = _payload(res)
    assert data["ok"] is False
    assert data["status_code"] == 409


async def test_cancel_generation_returns_to_shot_review(server, db_session_factory):
    pid = await seed_project(db_session_factory, status="shot_generating")
    async with Client(server) as c:
        res = await c.call_tool("cancel_generation", {"project_id": pid})
    assert _payload(res)["status"] == "shot_review"


async def test_cancel_generation_wrong_status_409(server, db_session_factory):
    pid = await seed_project(db_session_factory, status="draft")
    async with Client(server) as c:
        res = await c.call_tool("cancel_generation", {"project_id": pid})
    data = _payload(res)
    assert data["ok"] is False
    assert data["status_code"] == 409


async def test_get_generation_status(server, db_session_factory, monkeypatch):
    from sqlalchemy import select
    from app.models.project import Shot
    from tests.integration.conftest import install_fake_cos_credentials

    install_fake_cos_credentials(monkeypatch)

    pid = await seed_project(db_session_factory, status="shot_review")
    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(
            Shot.project_id == pid, Shot.shot_id == 1))).scalar_one()
        shot.status = "completed"
        # the API serializes video_path through to_media_url() into a signed
        # COS URL — must be a real key (projects/...), not a filesystem path,
        # or to_media_url() rejects it and has_video/video_path go null.
        shot.video_path = f"projects/{pid}/shots/shot_1/output.mp4"
        await s.commit()
    async with Client(server) as c:
        res = await c.call_tool("get_generation_status", {"project_id": pid})
    data = _payload(res)
    assert data["status"] == "shot_review"
    assert len(data["shots"]) == 3
    first = data["shots"][0]
    assert first["shot_id"] == 1
    assert first["has_video"] is True
    assert first["video_path"].startswith("http")
    assert "/api/media/" not in first["video_path"]
    assert set(first) == {"shot_id", "status", "has_video", "video_path",
                          "error_message", "vc_status", "tf_status"}
