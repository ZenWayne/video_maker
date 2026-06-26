"""Integration tests for upload-first-frame / upload-tail-frame endpoints (Task 5).

TDD: write tests first (RED), then implement the handlers (GREEN).
"""
import re
import pytest
from pathlib import Path
from sqlalchemy import select

from tests.integration.conftest import HEADERS, _make_project
from app.models.project import Shot

# Minimal valid PNG bytes (8-byte PNG signature, enough to be non-empty)
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde"
)

TS_UUID_RE = re.compile(r"\d+_[0-9a-f]{8}\.png$")


# ── helpers ──────────────────────────────────────────────────────────────────

async def _seed_shot(db_session_factory, project_id, shot_id=1):
    async with db_session_factory() as s:
        s.add(Shot(
            project_id=project_id,
            shot_id=shot_id,
            text="Test shot",
            shot_type="Medium Shot",
            visual_description="Test visual",
            shot_duration=6,
            status="completed",
            align_with_previous=False,
        ))
        await s.commit()


async def _get_shot(db_session_factory, project_id, shot_id=1):
    async with db_session_factory() as s:
        result = await s.execute(
            select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
        )
        return result.scalar_one()


# ── upload-first-frame tests ──────────────────────────────────────────────────

async def test_upload_first_frame_200(client, db_session_factory):
    """200 OK; returned URL ends in ts_uuid pattern."""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _seed_shot(db_session_factory, pid)

    r = await client.post(
        f"/api/projects/{pid}/shots/1/upload-first-frame",
        files={"file": ("f.png", PNG_BYTES, "image/png")},
        headers=HEADERS,
    )
    assert r.status_code == 200
    data = r.json()
    url = data["custom_first_frame_path"]
    assert TS_UUID_RE.search(url), f"URL {url!r} doesn't match ts_uuid pattern"


async def test_upload_first_frame_db_path(client, db_session_factory):
    """DB column is set to an absolute path whose basename matches ts_uuid."""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _seed_shot(db_session_factory, pid)

    await client.post(
        f"/api/projects/{pid}/shots/1/upload-first-frame",
        files={"file": ("f.png", PNG_BYTES, "image/png")},
        headers=HEADERS,
    )

    shot = await _get_shot(db_session_factory, pid)
    assert shot.custom_first_frame_path is not None
    basename = Path(shot.custom_first_frame_path).name
    assert TS_UUID_RE.match(basename), f"basename {basename!r} doesn't match ts_uuid"
    assert Path(shot.custom_first_frame_path).is_absolute()


async def test_upload_first_frame_file_exists(client, db_session_factory):
    """The file actually lands on disk at the DB-stored path."""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _seed_shot(db_session_factory, pid)

    await client.post(
        f"/api/projects/{pid}/shots/1/upload-first-frame",
        files={"file": ("f.png", PNG_BYTES, "image/png")},
        headers=HEADERS,
    )

    shot = await _get_shot(db_session_factory, pid)
    assert shot.custom_first_frame_path is not None
    assert Path(shot.custom_first_frame_path).exists()


async def test_upload_first_frame_shot_not_found(client, db_session_factory):
    """Returns 404 when the shot doesn't exist."""
    pid = await _make_project(db_session_factory, status="shot_review")

    r = await client.post(
        f"/api/projects/{pid}/shots/999/upload-first-frame",
        files={"file": ("f.png", PNG_BYTES, "image/png")},
        headers=HEADERS,
    )
    assert r.status_code == 404


async def test_upload_first_frame_project_not_found(client):
    """Returns 404 when the project doesn't exist."""
    r = await client.post(
        "/api/projects/nonexistent/shots/1/upload-first-frame",
        files={"file": ("f.png", PNG_BYTES, "image/png")},
        headers=HEADERS,
    )
    assert r.status_code == 404


# ── upload-tail-frame tests ───────────────────────────────────────────────────

async def test_upload_tail_frame_200(client, db_session_factory):
    """200 OK; returned URL ends in ts_uuid pattern; tf_status is 'done'."""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _seed_shot(db_session_factory, pid)

    r = await client.post(
        f"/api/projects/{pid}/shots/1/upload-tail-frame",
        files={"file": ("f.png", PNG_BYTES, "image/png")},
        headers=HEADERS,
    )
    assert r.status_code == 200
    data = r.json()
    url = data["target_last_frame_path"]
    assert TS_UUID_RE.search(url), f"URL {url!r} doesn't match ts_uuid pattern"
    assert data["tf_status"] == "done"


async def test_upload_tail_frame_db_path(client, db_session_factory):
    """DB columns target_last_frame_path and tf_status are set correctly."""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _seed_shot(db_session_factory, pid)

    await client.post(
        f"/api/projects/{pid}/shots/1/upload-tail-frame",
        files={"file": ("f.png", PNG_BYTES, "image/png")},
        headers=HEADERS,
    )

    shot = await _get_shot(db_session_factory, pid)
    assert shot.target_last_frame_path is not None
    basename = Path(shot.target_last_frame_path).name
    assert TS_UUID_RE.match(basename), f"basename {basename!r} doesn't match ts_uuid"
    assert Path(shot.target_last_frame_path).is_absolute()
    assert shot.tf_status == "done"


async def test_upload_tail_frame_file_exists(client, db_session_factory):
    """The file actually lands on disk at the DB-stored path."""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _seed_shot(db_session_factory, pid)

    await client.post(
        f"/api/projects/{pid}/shots/1/upload-tail-frame",
        files={"file": ("f.png", PNG_BYTES, "image/png")},
        headers=HEADERS,
    )

    shot = await _get_shot(db_session_factory, pid)
    assert shot.target_last_frame_path is not None
    assert Path(shot.target_last_frame_path).exists()


async def test_upload_tail_frame_shot_not_found(client, db_session_factory):
    """Returns 404 when the shot doesn't exist."""
    pid = await _make_project(db_session_factory, status="shot_review")

    r = await client.post(
        f"/api/projects/{pid}/shots/999/upload-tail-frame",
        files={"file": ("f.png", PNG_BYTES, "image/png")},
        headers=HEADERS,
    )
    assert r.status_code == 404


async def test_upload_tail_frame_project_not_found(client):
    """Returns 404 when the project doesn't exist."""
    r = await client.post(
        "/api/projects/nonexistent/shots/1/upload-tail-frame",
        files={"file": ("f.png", PNG_BYTES, "image/png")},
        headers=HEADERS,
    )
    assert r.status_code == 404
