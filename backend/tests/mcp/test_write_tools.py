import json
import pytest
from fastmcp import Client
from sqlalchemy import select
from app.models.project import Shot
from tests.mcp.conftest import seed_project


def _payload(result):
    if getattr(result, "data", None) is not None:
        return result.data
    return json.loads(result.content[0].text)


@pytest.fixture
def server(backend):
    from mcp_server.server import create_server
    return create_server(backend)


async def test_update_dialogue(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        res = await c.call_tool("update_dialogue",
                                {"project_id": pid, "shot_id": 1, "text": "new dialogue here"})
    data = _payload(res)
    assert data["shot"]["text"] == "new dialogue here"
    assert "within_range" in data["word_count"]
    assert data["note"] is None


async def test_update_dialogue_note_fires_when_video_exists(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    # Set video_path on shot 1 so the "won't change existing video" note fires
    async with db_session_factory() as session:
        result = await session.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )
        shot = result.scalar_one()
        shot.video_path = "/fake/output.mp4"
        await session.commit()
    async with Client(server) as c:
        res = await c.call_tool("update_dialogue",
                                {"project_id": pid, "shot_id": 1, "text": "updated line"})
    data = _payload(res)
    assert data["note"] and "regenerated" in data["note"]


async def test_update_dialogue_rejects_empty(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        with pytest.raises(Exception, match="empty"):
            await c.call_tool("update_dialogue",
                              {"project_id": pid, "shot_id": 1, "text": "   "})


async def test_update_motion_appends_lip_marker(server, db_session_factory):
    pid = await seed_project(db_session_factory)  # shot 1 text = "line 1"
    async with Client(server) as c:
        res = await c.call_tool("update_motion",
                                {"project_id": pid, "shot_id": 1,
                                 "motion_prompt": "slow zoom in", "sync_lip_marker": True})
    data = _payload(res)
    assert "slow zoom in" in data["shot"]["motion_prompt"]
    assert 'The character says: "line 1"' in data["shot"]["motion_prompt"]


async def test_update_motion_no_marker_when_disabled(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        res = await c.call_tool("update_motion",
                                {"project_id": pid, "shot_id": 1,
                                 "motion_prompt": "pan left", "sync_lip_marker": False})
    data = _payload(res)
    assert data["shot"]["motion_prompt"] == "pan left"


async def test_update_motion_no_text_shot(server, db_session_factory):
    """When shot has no dialogue (text == ""), sync_lip_marker must NOT append a lip-sync line."""
    pid = await seed_project(db_session_factory)
    # Clear text on shot 1 so there is no dialogue
    async with db_session_factory() as session:
        result = await session.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )
        shot = result.scalar_one()
        shot.text = ""
        await session.commit()
    async with Client(server) as c:
        res = await c.call_tool("update_motion",
                                {"project_id": pid, "shot_id": 1,
                                 "motion_prompt": "slow zoom in", "sync_lip_marker": True})
    data = _payload(res)
    # No dialogue → postprocess_motion_prompt skipped → raw prompt stored unchanged
    assert data["shot"]["motion_prompt"] == "slow zoom in"


async def test_batch_update_shots_partial(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        res = await c.call_tool("batch_update_shots", {
            "project_id": pid,
            "updates": [
                {"shot_id": 1, "text": "t1", "motion_prompt": "m1"},
                {"shot_id": 999, "text": "bad"},  # nonexistent → fails this item only
            ],
        })
    results = _payload(res)["results"]
    by_id = {r["shot_id"]: r for r in results}
    assert by_id[1]["ok"] is True
    assert by_id[999]["ok"] is False
    assert "error" in by_id[999]


async def test_batch_update_shots_missing_shot_id(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        res = await c.call_tool("batch_update_shots", {
            "project_id": pid,
            "updates": [
                {"shot_id": 1, "text": "valid item"},
                {"text": "x"},  # missing shot_id → per-item failure, not batch abort
            ],
        })
    results = _payload(res)["results"]
    # valid item succeeds
    valid = next(r for r in results if r["shot_id"] == 1)
    assert valid["ok"] is True
    # malformed item recorded as failure with shot_id None
    failed = next(r for r in results if r["shot_id"] is None)
    assert failed["ok"] is False


async def test_replace_storyboard_tool(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        res = await c.call_tool("replace_storyboard", {
            "project_id": pid,
            "scene_overview": "fresh",
            "shots": [
                {"shot_id": 1, "text": "only one", "shot_type": "Close-up",
                 "visual_description": "v", "shot_duration": 4, "align_with_previous": False},
            ],
        })
    data = _payload(res)
    assert data["ok"] is True
    # verify via read
    async with Client(server) as c:
        shots = _payload(await c.call_tool("list_shots", {"project_id": pid}))
    assert [s["shot_id"] for s in shots] == [1]
