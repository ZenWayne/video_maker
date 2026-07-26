"""CC 候选化：校准产候选，不直写 last_frame；失败标记 cc_status（真实 COS）。"""
import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy import select

from tests.integration.conftest import _make_project, _add_shot
from tests.integration.conftest_cos import requires_cos
from app.models.project import ImageCandidate, Shot
from app.services import object_store

pytestmark = requires_cos


async def _seed_shot_with_last_frame(sf, tmp_path, pid):
    key = f"projects/{pid}/shots/shot_1/last_frame_1_aaaa.png"
    f = tmp_path / "lf.png"; f.write_bytes(b"LF")
    await object_store.put(key, f)
    async with sf() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.last_frame_path = key
        shot.pristine_last_frame_key = key
        await s.commit()
    return key


async def _seed_char_ref(tmp_path, pid):
    key = f"projects/{pid}/reference_images/char_ref.jpg"
    f = tmp_path / "ref.jpg"; f.write_bytes(b"REF")
    await object_store.put(key, f)
    return key


async def test_calibrate_creates_candidate_not_replace(
    db_session_factory, redis, tmp_path, cos_prefix
):
    from worker.tasks import _do_character_calibrate_one

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    lf_key = await _seed_shot_with_last_frame(db_session_factory, tmp_path, pid)
    ref_key = await _seed_char_ref(tmp_path, pid)

    async def _fake_cc(refs, src, out):
        from pathlib import Path
        Path(out).write_bytes(b"CC")
        return out

    with patch("app.services.image_generation.calibrate_face", new=AsyncMock(side_effect=_fake_cc)):
        await _do_character_calibrate_one(
            db_session_factory, redis, pid, 1, [ref_key]
        )

    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        cand = (await s.execute(select(ImageCandidate))).scalar_one()
        assert shot.last_frame_path == lf_key          # 不直写
        assert shot.cc_status is None
        assert cand.slot == "cc" and cand.status == "done"
        assert "candidates/" in cand.file_path
        got = tmp_path / "got.png"
        await object_store.get(cand.file_path, got)
        assert got.read_bytes() == b"CC"


async def test_calibrate_preserves_cc_status_when_last_frame_already_adopted(
    db_session_factory, redis, tmp_path, cos_prefix
):
    """last_frame 已是已采纳的 cc_*.png 校准帧时，再次校准出候选不应清空 cc_status
    （否则 已校准/还原 UI 消失、character-calibrate-revert 400）。"""
    from worker.tasks import _do_character_calibrate_one

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    ref_key = await _seed_char_ref(tmp_path, pid)

    lf_key = f"projects/{pid}/shots/shot_1/cc_0_prev.png"
    lf = tmp_path / "cc_prev.png"; lf.write_bytes(b"OLD_CC")
    await object_store.put(lf_key, lf)
    pristine_key = f"projects/{pid}/shots/shot_1/last_frame_pristine.png"
    pristine = tmp_path / "pristine.png"; pristine.write_bytes(b"PRISTINE")
    await object_store.put(pristine_key, pristine)
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.last_frame_path = lf_key
        shot.pristine_last_frame_key = pristine_key
        shot.cc_status = "done"
        await s.commit()

    async def _fake_cc(refs, src, out):
        from pathlib import Path
        Path(out).write_bytes(b"CC")
        return out

    with patch("app.services.image_generation.calibrate_face", new=AsyncMock(side_effect=_fake_cc)):
        await _do_character_calibrate_one(
            db_session_factory, redis, pid, 1, [ref_key]
        )

    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        cand = (await s.execute(select(ImageCandidate))).scalar_one()
        assert cand.slot == "cc" and cand.status == "done"
        assert shot.cc_status == "done"  # 还原链保持可用


async def test_calibrate_failure_marks_candidate_and_cc_status(
    db_session_factory, redis, tmp_path, cos_prefix
):
    from worker.tasks import _do_character_calibrate_one

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await _seed_shot_with_last_frame(db_session_factory, tmp_path, pid)
    ref_key = await _seed_char_ref(tmp_path, pid)

    with patch("app.services.image_generation.calibrate_face", new=AsyncMock(side_effect=RuntimeError("boom"))):
        with pytest.raises(RuntimeError):
            await _do_character_calibrate_one(
                db_session_factory, redis, pid, 1, [ref_key]
            )

    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        cand = (await s.execute(select(ImageCandidate))).scalar_one()
        assert cand.status == "failed" and "boom" in cand.error
        assert shot.cc_status == "failed"
