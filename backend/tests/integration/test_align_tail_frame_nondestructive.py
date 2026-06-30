"""Integration tests for non-destructive align-tail-frame endpoint.

POST /api/projects/{pid}/shots/{sid}/align-tail-frame must:
  - set shot.trim_frames in the DB (metadata only)
  - leave the source output_*.mp4 byte-identical (never modified)
  - NOT create any trimmed_*.mp4 file
  - refresh last_frame and reset CC state
  - return aligned_to_frame == the mocked best frame value
"""
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from .conftest import HEADERS, _make_project, _add_shot, seed_shot_with_source


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_align_tail_frame_metadata_only_no_trimmed_file(
    client, db_session_factory
):
    """align-tail-frame should only update trim_frames in DB; source file must be unchanged
    and no trimmed_*.mp4 should appear in the shot directory."""
    pid = await _make_project(db_session_factory, status="completed")
    await _add_shot(db_session_factory, pid, shot_id=1, status="completed")
    source = await seed_shot_with_source(db_session_factory, pid, 1)

    # Set a target_last_frame_path so the endpoint proceeds
    target_lf = source.parent / "target_last_frame.png"
    target_lf.write_bytes(b"fake-target-frame")
    from sqlalchemy import select
    from app.models.project import Shot
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.target_last_frame_path = str(target_lf)
        await s.commit()

    before_md5 = _md5(source)

    with patch("app.agents.video_trimmer.find_best_tail_frame", return_value=40):
        r = await client.post(
            f"/api/projects/{pid}/shots/1/align-tail-frame",
            headers=HEADERS,
        )

    assert r.status_code == 200
    body = r.json()
    assert body["trim_frames"] == 40
    assert body["aligned_to_frame"] == 40

    # Source video must be byte-identical
    assert _md5(source) == before_md5, "Source file was mutated — must be immutable"

    # No trimmed_ files must have been created
    assert not list(source.parent.glob("trimmed_*.mp4")), \
        "trimmed_*.mp4 was created — endpoint must be non-destructive"


@pytest.mark.asyncio
async def test_align_tail_frame_resets_cc(client, db_session_factory):
    """align-tail-frame must clear cc_status and write a new last_frame_*.png."""
    from sqlalchemy import select
    from app.models.project import Shot

    pid = await _make_project(db_session_factory, status="completed")
    await _add_shot(db_session_factory, pid, shot_id=1, status="completed")
    source = await seed_shot_with_source(db_session_factory, pid, 1)

    # Set target_last_frame_path and simulate a prior CC run
    target_lf = source.parent / "target_last_frame.png"
    target_lf.write_bytes(b"fake")
    pre_cc = source.parent / "last_frame_pre_cc.png"
    pre_cc.write_bytes(b"fake-pre-cc")

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.target_last_frame_path = str(target_lf)
        shot.cc_status = "done"
        await s.commit()

    with patch("app.agents.video_trimmer.find_best_tail_frame", return_value=40):
        r = await client.post(
            f"/api/projects/{pid}/shots/1/align-tail-frame",
            headers=HEADERS,
        )
    assert r.status_code == 200

    # CC state cleared
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        assert shot.cc_status is None
        assert shot.trim_frames == 40
        assert shot.last_frame_path is not None
        assert Path(shot.last_frame_path).exists()

    # pre-CC backup must be deleted
    assert not pre_cc.exists(), "last_frame_pre_cc.png should have been removed"


@pytest.mark.asyncio
async def test_align_tail_frame_no_target_returns_400(client, db_session_factory):
    """Without target_last_frame_path, endpoint must return 400."""
    pid = await _make_project(db_session_factory, status="completed")
    await _add_shot(db_session_factory, pid, shot_id=1, status="completed")
    await seed_shot_with_source(db_session_factory, pid, 1)

    r = await client.post(
        f"/api/projects/{pid}/shots/1/align-tail-frame",
        headers=HEADERS,
    )
    assert r.status_code == 400
