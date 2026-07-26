"""Integration tests for reference image upload/delete endpoints.

Uploads now publish to real COS (Task 10). Only the tests that actually reach
the storage layer (i.e. take the cos_prefix fixture) are marked @requires_cos
individually — the 404/400 guard tests below return before any object_store
call (see uploads.py: kind validation, project 404, image 404 all precede
storage access) and must stay unconditionally runnable so they keep providing
regression coverage in credential-less dev/CI environments. A file-level
pytestmark here would silently strip that coverage (the Task 4 mistake this
project already paid for once — see conftest_cos.py's requires_cos docstring
lineage in tests/unit vs tests/integration placement rules).
"""
import pytest
from tests.integration.conftest import HEADERS
from tests.integration.conftest_cos import requires_cos


@requires_cos
async def test_upload_character_image(client, project_in_draft, cos_prefix):
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


@requires_cos
async def test_upload_scene_image(client, project_in_draft, cos_prefix):
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


@requires_cos
async def test_upload_multiple_images(client, project_in_draft, cos_prefix):
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


@requires_cos
async def test_delete_reference_image(client, project_in_draft, cos_prefix):
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
