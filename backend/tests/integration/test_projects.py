"""Integration tests for project CRUD endpoints."""
import pytest
from tests.integration.conftest import HEADERS, _make_project, _add_shots


async def test_create_project_success(client):
    r = await client.post(
        "/api/projects",
        json={"title": "My Video", "theme_text": "Adventure theme"},
        headers=HEADERS,
    )
    assert r.status_code == 201
    data = r.json()
    assert data["title"] == "My Video"
    assert data["status"] == "draft"
    assert "id" in data
    assert data["creator_name"] == "test-user"


async def test_create_project_no_user_header(client):
    r = await client.post(
        "/api/projects",
        json={"title": "Title", "theme_text": "Theme"},
    )
    assert r.status_code == 400


async def test_create_project_blank_title(client):
    r = await client.post(
        "/api/projects",
        json={"title": "", "theme_text": "Theme"},
        headers=HEADERS,
    )
    assert r.status_code == 422


async def test_create_project_blank_theme(client):
    r = await client.post(
        "/api/projects",
        json={"title": "Title", "theme_text": ""},
        headers=HEADERS,
    )
    assert r.status_code == 422


async def test_get_project_success(client, project_in_draft):
    r = await client.get(f"/api/projects/{project_in_draft['id']}")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == project_in_draft["id"]
    assert data["title"] == project_in_draft["title"]
    assert data["status"] == "draft"


async def test_get_project_not_found(client):
    r = await client.get("/api/projects/nonexistent-id")
    assert r.status_code == 404


async def test_list_projects_empty(client):
    r = await client.get("/api/projects")
    assert r.status_code == 200
    data = r.json()
    assert data["items"] == []
    assert data["total"] == 0


async def test_list_projects_returns_created(client, project_in_draft):
    r = await client.get("/api/projects")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    assert data["items"][0]["id"] == project_in_draft["id"]


async def test_list_projects_filter_by_status(client, project_in_draft):
    r = await client.get("/api/projects?status=draft")
    assert r.status_code == 200
    assert r.json()["total"] == 1

    r2 = await client.get("/api/projects?status=scripting")
    assert r2.json()["total"] == 0


async def test_list_projects_pagination(client, make_project):
    await make_project(title="Project A")
    await make_project(title="Project B")

    r = await client.get("/api/projects?limit=1&offset=0")
    assert r.status_code == 200
    data = r.json()
    assert len(data["items"]) == 1
    assert data["total"] == 2
    assert data["limit"] == 1
    assert data["offset"] == 0


async def test_delete_project_success(client, project_in_draft):
    pid = project_in_draft["id"]
    r = await client.delete(f"/api/projects/{pid}")
    assert r.status_code == 204

    r2 = await client.get(f"/api/projects/{pid}")
    assert r2.status_code == 404


async def test_delete_project_not_found(client):
    r = await client.delete("/api/projects/nonexistent-id")
    assert r.status_code == 404


async def test_get_script_success(client, project_in_script_review):
    pid = project_in_script_review
    r = await client.get(f"/api/projects/{pid}/script")
    assert r.status_code == 200
    data = r.json()
    assert data["project_id"] == pid
    assert data["status"] == "script_review"
    assert len(data["shots"]) == 3
    shot = data["shots"][0]
    assert "shot_id" in shot
    assert "text" in shot
    assert "shot_type" in shot


async def test_get_script_not_found(client):
    r = await client.get("/api/projects/nonexistent/script")
    assert r.status_code == 404
