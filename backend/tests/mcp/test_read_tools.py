import json
import pytest
from fastmcp import Client
from tests.mcp.conftest import seed_project


def _payload(result):
    """Extract the structured/text payload from a FastMCP tool result."""
    if getattr(result, "data", None) is not None:
        return result.data
    return json.loads(result.content[0].text)


@pytest.fixture
def server(backend):
    from mcp_server.server import create_server
    return create_server(backend)


async def test_list_projects_tool(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        res = await c.call_tool("list_projects", {})
    ids = [p["id"] for p in _payload(res)]
    assert pid in ids


async def test_get_project_tool(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        res = await c.call_tool("get_project", {"project_id": pid})
    data = _payload(res)
    assert data["id"] == pid
    assert data["shot_count"] == 3
    assert "theme" in data


async def test_list_shots_tool(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        res = await c.call_tool("list_shots", {"project_id": pid})
    shots = _payload(res)
    assert [s["shot_id"] for s in shots] == [1, 2, 3]
    assert shots[0]["word_count_target"] == [13, 16]  # duration 6


async def test_get_shot_tool_with_neighbors(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        res = await c.call_tool("get_shot", {"project_id": pid, "shot_id": 2})
    data = _payload(res)
    assert data["shot_id"] == 2
    assert data["prev_text"] == "line 1"
    assert data["next_text"] == "line 3"


async def test_guidelines_tool(server):
    async with Client(server) as c:
        res = await c.call_tool("get_authoring_guidelines", {})
    text = _payload(res)
    assert "motion_prompt" in text
