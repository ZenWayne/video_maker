"""Integration tests for extract-first-frame / extract-last-frame endpoints (Task 7).

TDD: write tests first (RED), then implement the handlers (GREEN).

For each endpoint we assert:
1. 200; returned URL basename matches ts_uuid pattern AND is a distinct path from source.
2. DB field set to new path; new file exists on disk.
3. Source file STILL exists (copy, not move).
4. (last-frame only) tf_status == "done" in response and DB.
5. Missing/empty source → 400  (two variants: field None; field set but file absent).
6. 404 when shot doesn't exist.
"""
import re
import pytest
from pathlib import Path
from sqlalchemy import select

from tests.integration.conftest import HEADERS, _make_project
from app.models.project import Shot

# ts_uuid pattern: <unix_seconds>_<8hex>.<ext>
TS_UUID_RE = re.compile(r"\d+_[0-9a-f]{8}\.[a-z]+$")

# Minimal PNG bytes for source file
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde"
)


# ── helpers ───────────────────────────────────────────────────────────────────

async def _seed_shot(db_session_factory, project_id, shot_id=1, **extra):
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
            **extra,
        ))
        await s.commit()


async def _get_shot(db_session_factory, project_id, shot_id=1):
    async with db_session_factory() as s:
        result = await s.execute(
            select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
        )
        return result.scalar_one()


def _write_source_file(tmp_path: Path, name: str = "source.png") -> Path:
    """Create a real source file under tmp_path to use as first/last frame."""
    src = tmp_path / name
    src.write_bytes(PNG_BYTES)
    return src


# ══════════════════════════════════════════════════════════════════════════════
# extract-first-frame
# ══════════════════════════════════════════════════════════════════════════════

async def test_extract_first_frame_200_distinct_ts_uuid(client, db_session_factory, tmp_path):
    """200; returned URL basename is ts_uuid and DISTINCT from source path."""
    pid = await _make_project(db_session_factory, status="shot_review")
    src = _write_source_file(tmp_path, "first_frame.png")
    await _seed_shot(db_session_factory, pid, first_frame_path=str(src))

    r = await client.post(
        f"/api/projects/{pid}/shots/1/extract-first-frame",
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    url = data["custom_first_frame_path"]
    assert TS_UUID_RE.search(url), f"URL {url!r} doesn't match ts_uuid pattern"
    # Distinct from source
    assert Path(url).name != src.name, "dest filename must differ from source filename"


async def test_extract_first_frame_db_and_file_exist(client, db_session_factory, tmp_path):
    """DB field set; new file exists on disk."""
    pid = await _make_project(db_session_factory, status="shot_review")
    src = _write_source_file(tmp_path, "first_frame.png")
    await _seed_shot(db_session_factory, pid, first_frame_path=str(src))

    await client.post(
        f"/api/projects/{pid}/shots/1/extract-first-frame",
        headers=HEADERS,
    )

    shot = await _get_shot(db_session_factory, pid)
    assert shot.custom_first_frame_path is not None
    assert TS_UUID_RE.search(Path(shot.custom_first_frame_path).name), \
        f"DB path basename {Path(shot.custom_first_frame_path).name!r} doesn't match ts_uuid"
    assert Path(shot.custom_first_frame_path).exists(), "Dest file must exist on disk"


async def test_extract_first_frame_source_still_exists(client, db_session_factory, tmp_path):
    """Source first_frame_path must NOT be deleted (copy, not move)."""
    pid = await _make_project(db_session_factory, status="shot_review")
    src = _write_source_file(tmp_path, "first_frame.png")
    await _seed_shot(db_session_factory, pid, first_frame_path=str(src))

    await client.post(
        f"/api/projects/{pid}/shots/1/extract-first-frame",
        headers=HEADERS,
    )

    assert src.exists(), "Source first_frame_path must remain after extract (copy, not move)"


async def test_extract_first_frame_400_when_field_none(client, db_session_factory, tmp_path):
    """400 when first_frame_path is None (field empty)."""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _seed_shot(db_session_factory, pid)  # first_frame_path defaults to None

    r = await client.post(
        f"/api/projects/{pid}/shots/1/extract-first-frame",
        headers=HEADERS,
    )
    assert r.status_code == 400, r.text


async def test_extract_first_frame_400_when_file_absent(client, db_session_factory, tmp_path):
    """400 when first_frame_path is set but the file doesn't exist on disk."""
    pid = await _make_project(db_session_factory, status="shot_review")
    ghost_path = tmp_path / "ghost_first_frame.png"  # NOT created on disk
    await _seed_shot(db_session_factory, pid, first_frame_path=str(ghost_path))

    r = await client.post(
        f"/api/projects/{pid}/shots/1/extract-first-frame",
        headers=HEADERS,
    )
    assert r.status_code == 400, r.text


async def test_extract_first_frame_404_shot_missing(client, db_session_factory):
    """404 when shot doesn't exist."""
    pid = await _make_project(db_session_factory, status="shot_review")

    r = await client.post(
        f"/api/projects/{pid}/shots/999/extract-first-frame",
        headers=HEADERS,
    )
    assert r.status_code == 404, r.text


# ══════════════════════════════════════════════════════════════════════════════
# extract-last-frame
# ══════════════════════════════════════════════════════════════════════════════

async def test_extract_last_frame_200_distinct_ts_uuid(client, db_session_factory, tmp_path):
    """200; returned URL basename is ts_uuid and DISTINCT from source path; tf_status=done."""
    pid = await _make_project(db_session_factory, status="shot_review")
    src = _write_source_file(tmp_path, "last_frame.png")
    await _seed_shot(db_session_factory, pid, last_frame_path=str(src))

    r = await client.post(
        f"/api/projects/{pid}/shots/1/extract-last-frame",
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    url = data["target_last_frame_path"]
    assert TS_UUID_RE.search(url), f"URL {url!r} doesn't match ts_uuid pattern"
    assert Path(url).name != src.name, "dest filename must differ from source filename"
    assert data["tf_status"] == "done"


async def test_extract_last_frame_db_and_file_exist(client, db_session_factory, tmp_path):
    """DB fields (target_last_frame_path, tf_status) set; new file exists on disk."""
    pid = await _make_project(db_session_factory, status="shot_review")
    src = _write_source_file(tmp_path, "last_frame.png")
    await _seed_shot(db_session_factory, pid, last_frame_path=str(src))

    await client.post(
        f"/api/projects/{pid}/shots/1/extract-last-frame",
        headers=HEADERS,
    )

    shot = await _get_shot(db_session_factory, pid)
    assert shot.target_last_frame_path is not None
    assert TS_UUID_RE.search(Path(shot.target_last_frame_path).name), \
        f"DB path basename {Path(shot.target_last_frame_path).name!r} doesn't match ts_uuid"
    assert Path(shot.target_last_frame_path).exists(), "Dest file must exist on disk"
    assert shot.tf_status == "done"


async def test_extract_last_frame_source_still_exists(client, db_session_factory, tmp_path):
    """Source last_frame_path must NOT be deleted (copy, not move)."""
    pid = await _make_project(db_session_factory, status="shot_review")
    src = _write_source_file(tmp_path, "last_frame.png")
    await _seed_shot(db_session_factory, pid, last_frame_path=str(src))

    await client.post(
        f"/api/projects/{pid}/shots/1/extract-last-frame",
        headers=HEADERS,
    )

    assert src.exists(), "Source last_frame_path must remain after extract (copy, not move)"


async def test_extract_last_frame_400_when_field_none(client, db_session_factory, tmp_path):
    """400 when last_frame_path is None (field empty)."""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _seed_shot(db_session_factory, pid)  # last_frame_path defaults to None

    r = await client.post(
        f"/api/projects/{pid}/shots/1/extract-last-frame",
        headers=HEADERS,
    )
    assert r.status_code == 400, r.text


async def test_extract_last_frame_400_when_file_absent(client, db_session_factory, tmp_path):
    """400 when last_frame_path is set but the file doesn't exist on disk."""
    pid = await _make_project(db_session_factory, status="shot_review")
    ghost_path = tmp_path / "ghost_last_frame.png"  # NOT created on disk
    await _seed_shot(db_session_factory, pid, last_frame_path=str(ghost_path))

    r = await client.post(
        f"/api/projects/{pid}/shots/1/extract-last-frame",
        headers=HEADERS,
    )
    assert r.status_code == 400, r.text


async def test_extract_last_frame_404_shot_missing(client, db_session_factory):
    """404 when shot doesn't exist."""
    pid = await _make_project(db_session_factory, status="shot_review")

    r = await client.post(
        f"/api/projects/{pid}/shots/999/extract-last-frame",
        headers=HEADERS,
    )
    assert r.status_code == 404, r.text
