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

# test_extract_first_frame_200_distinct_ts_uuid / _db_and_file_exist /
# _source_still_exists moved to the real-COS test
# tests/integration/test_uploads_oss.py::test_extract_first_frame_copies_resolved_source_to_custom_frames
# — custom_first_frame_path now holds a COS key (Task 10); pick_first_frame()
# returns a Path WRAPPING a key, and the endpoint must object_store.copy() it,
# not run a local .exists()/shutil.copy2 on a fabricated tmp_path file.


async def test_extract_first_frame_400_when_field_none(client, db_session_factory, tmp_path):
    """400 when there is no resolvable first frame (no custom / prev / char ref)."""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _seed_shot(db_session_factory, pid)  # no custom first frame, no prev shot, no character ref -> unresolvable

    r = await client.post(
        f"/api/projects/{pid}/shots/1/extract-first-frame",
        headers=HEADERS,
    )
    assert r.status_code == 400, r.text


# test_extract_first_frame_400_when_file_absent moved to the real-COS test
# tests/integration/test_uploads_oss.py::test_extract_first_frame_400_when_key_absent
# — same reasoning as the last_frame variant above.


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

# test_extract_last_frame_200_distinct_ts_uuid / _db_and_file_exist /
# _source_still_exists moved to the real-COS test
# tests/integration/test_uploads_oss.py::test_extract_last_frame_copies_to_new_key
# — same reasoning as the first_frame group above (last_frame_path is a COS key,
# not a local path; the endpoint object_store.copy()s it).


async def test_extract_last_frame_400_when_field_none(client, db_session_factory, tmp_path):
    """400 when last_frame_path is None (field empty)."""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _seed_shot(db_session_factory, pid)  # last_frame_path defaults to None

    r = await client.post(
        f"/api/projects/{pid}/shots/1/extract-last-frame",
        headers=HEADERS,
    )
    assert r.status_code == 400, r.text


# test_extract_last_frame_400_when_file_absent moved to the real-COS test
# tests/integration/test_uploads_oss.py::test_extract_last_frame_400_when_key_absent
# — last_frame_path now holds a COS key (Task 10), and "absent" must be checked
# via object_store.exists(), not a local Path().exists() on a fabricated path.


async def test_extract_last_frame_404_shot_missing(client, db_session_factory):
    """404 when shot doesn't exist."""
    pid = await _make_project(db_session_factory, status="shot_review")

    r = await client.post(
        f"/api/projects/{pid}/shots/999/extract-last-frame",
        headers=HEADERS,
    )
    assert r.status_code == 404, r.text


# ══════════════════════════════════════════════════════════════════════════════
# use-prev-last-frame  (提取上一镜末帧 → 本镜首帧)
# ══════════════════════════════════════════════════════════════════════════════

# test_use_prev_last_frame_copies_prev_tail moved to the real-COS test
# tests/integration/test_uploads_oss.py::test_use_prev_last_frame_copies_to_new_key
# — same reasoning: last_frame_path is a COS key, not a local path.


async def test_use_prev_last_frame_first_shot_400(client, db_session_factory):
    """Shot 1 has no previous shot → 400."""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _seed_shot(db_session_factory, pid, shot_id=1)

    r = await client.post(f"/api/projects/{pid}/shots/1/use-prev-last-frame", headers=HEADERS)
    assert r.status_code == 400, r.text


async def test_use_prev_last_frame_prev_no_tail_400(client, db_session_factory):
    """Previous shot has no last frame → 400."""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _seed_shot(db_session_factory, pid, shot_id=1)  # no last_frame_path
    await _seed_shot(db_session_factory, pid, shot_id=2)

    r = await client.post(f"/api/projects/{pid}/shots/2/use-prev-last-frame", headers=HEADERS)
    assert r.status_code == 400, r.text
