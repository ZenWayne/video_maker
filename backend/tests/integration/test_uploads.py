"""Integration tests for reference image upload/delete endpoints."""
import pytest
from tests.integration.conftest import HEADERS


async def test_upload_character_image(client, project_in_draft):
    pid = project_in_draft["id"]
    r = await client.post(
        f"/api/projects/{pid}/reference-images",
        data={"kind": "character"},
        files=[("files", ("test.jpg", b"fake-image-bytes", "image/jpeg"))],
    )
    assert r.status_code == 201
    data = r.json()
    assert len(data) == 1
    assert data[0]["kind"] == "character"
    assert data[0]["filename"] == "test.jpg"
    assert "id" in data[0]


async def test_upload_scene_image(client, project_in_draft):
    pid = project_in_draft["id"]
    r = await client.post(
        f"/api/projects/{pid}/reference-images",
        data={"kind": "scene"},
        files=[("files", ("scene.jpg", b"fake-scene-bytes", "image/jpeg"))],
    )
    assert r.status_code == 201
    assert r.json()[0]["kind"] == "scene"


async def test_upload_invalid_kind(client, project_in_draft):
    pid = project_in_draft["id"]
    r = await client.post(
        f"/api/projects/{pid}/reference-images",
        data={"kind": "invalid"},
        files=[("files", ("test.jpg", b"fake", "image/jpeg"))],
    )
    assert r.status_code == 400


async def test_upload_project_not_found(client):
    r = await client.post(
        "/api/projects/nonexistent/reference-images",
        data={"kind": "character"},
        files=[("files", ("test.jpg", b"fake", "image/jpeg"))],
    )
    assert r.status_code == 404


async def test_upload_multiple_images(client, project_in_draft):
    pid = project_in_draft["id"]
    r = await client.post(
        f"/api/projects/{pid}/reference-images",
        data={"kind": "character"},
        files=[
            ("files", ("a.jpg", b"img1-bytes", "image/jpeg")),
            ("files", ("b.jpg", b"img2-bytes", "image/jpeg")),
        ],
    )
    assert r.status_code == 201
    data = r.json()
    assert len(data) == 2
    assert data[0]["order_index"] == 0
    assert data[1]["order_index"] == 1


async def test_delete_reference_image(client, project_in_draft):
    pid = project_in_draft["id"]
    # Upload first
    upload_r = await client.post(
        f"/api/projects/{pid}/reference-images",
        data={"kind": "character"},
        files=[("files", ("test.jpg", b"fake", "image/jpeg"))],
    )
    image_id = upload_r.json()[0]["id"]

    r = await client.delete(f"/api/projects/{pid}/reference-images/{image_id}")
    assert r.status_code == 204


async def test_delete_reference_image_not_found(client, project_in_draft):
    pid = project_in_draft["id"]
    r = await client.delete(f"/api/projects/{pid}/reference-images/nonexistent-id")
    assert r.status_code == 404
