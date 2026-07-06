"""CC 候选化：校准产候选，不直写 last_frame；失败标记 cc_status."""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from sqlalchemy import select

from tests.integration.conftest import _make_project, _add_shot, _add_character_image
from app.models.project import ImageCandidate, Shot
from app.services.storage import shot_dir


async def _seed_shot_with_last_frame(sf, monkeypatch, tmp_path, pid):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    s_dir = shot_dir(pid, 1); s_dir.mkdir(parents=True, exist_ok=True)
    lf = s_dir / "last_frame_1_aaaa.png"; lf.write_bytes(b"LF")
    async with sf() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.last_frame_path = str(lf)
        await s.commit()
    return lf


async def test_calibrate_creates_candidate_not_replace(db_session_factory, redis, monkeypatch, tmp_path):
    from worker.tasks import _do_character_calibrate_one

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    lf = await _seed_shot_with_last_frame(db_session_factory, monkeypatch, tmp_path, pid)

    async def _fake_cc(refs, src, out):
        Path(out).write_bytes(b"CC")
        return out

    with patch("app.services.image_generation.calibrate_face", new=AsyncMock(side_effect=_fake_cc)):
        await _do_character_calibrate_one(
            db_session_factory, redis, pid, 1, ["/fake/ref.jpg"]
        )

    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        cand = (await s.execute(select(ImageCandidate))).scalar_one()
        assert shot.last_frame_path == str(lf)          # 不直写
        assert shot.cc_status is None
        assert cand.slot == "cc" and cand.status == "done"
        assert "candidates" in cand.file_path
        assert Path(cand.file_path).read_bytes() == b"CC"


async def test_calibrate_preserves_cc_status_when_last_frame_already_adopted(
    db_session_factory, redis, monkeypatch, tmp_path
):
    """last_frame 已是已采纳的 cc_*.png 校准帧时，再次校准出候选不应清空 cc_status
    （否则 已校准/还原 UI 消失、character-calibrate-revert 400）。"""
    from worker.tasks import _do_character_calibrate_one
    from app.config import settings

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)

    s_dir = shot_dir(pid, 1); s_dir.mkdir(parents=True, exist_ok=True)
    lf = s_dir / "cc_0_prev.png"; lf.write_bytes(b"OLD_CC")
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.last_frame_path = str(lf)
        shot.cc_status = "done"
        await s.commit()

    async def _fake_cc(refs, src, out):
        Path(out).write_bytes(b"CC")
        return out

    with patch("app.services.image_generation.calibrate_face", new=AsyncMock(side_effect=_fake_cc)):
        await _do_character_calibrate_one(
            db_session_factory, redis, pid, 1, ["/fake/ref.jpg"]
        )

    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        cand = (await s.execute(select(ImageCandidate))).scalar_one()
        assert cand.slot == "cc" and cand.status == "done"
        assert shot.cc_status == "done"  # 还原链保持可用


async def test_calibrate_failure_marks_candidate_and_cc_status(db_session_factory, redis, monkeypatch, tmp_path):
    from worker.tasks import _do_character_calibrate_one

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await _seed_shot_with_last_frame(db_session_factory, monkeypatch, tmp_path, pid)

    with patch("app.services.image_generation.calibrate_face", new=AsyncMock(side_effect=RuntimeError("boom"))):
        with pytest.raises(RuntimeError):
            await _do_character_calibrate_one(
                db_session_factory, redis, pid, 1, ["/fake/ref.jpg"]
            )

    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        cand = (await s.execute(select(ImageCandidate))).scalar_one()
        assert cand.status == "failed" and "boom" in cand.error
        assert shot.cc_status == "failed"
