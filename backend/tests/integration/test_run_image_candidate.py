"""run_image_candidate worker：模式路由、候选状态流转、事件（mock 全部生成函数）."""
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from sqlalchemy import select

from tests.integration.conftest import _make_project, _add_shot, _add_character_image
from app.models.project import ImageCandidate, Shot


@pytest.fixture
async def worker_ctx(db_session_factory, redis):
    return {"session_factory": db_session_factory, "redis": redis}


async def _seed_candidate(sf, pid, shot_id=1, slot="tail_frame", custom_prompt=None, ref_paths=None):
    async with sf() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == shot_id)
        )).scalar_one()
        c = ImageCandidate(
            project_id=pid, shot_pk=shot.id, shot_id=shot_id, slot=slot,
            custom_prompt=custom_prompt,
            prompt_source="custom" if custom_prompt else "auto",
            ref_paths=ref_paths,
        )
        s.add(c); await s.commit(); await s.refresh(c)
        return c.id


def _fake_gen(out_bytes=b"GEN"):
    async def _fake(*args, **kwargs):
        out = kwargs.get("output_path") or args[-1]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(out_bytes)
        return out
    return _fake


async def test_auto_tail_uses_generate_tail_frame(worker_ctx, db_session_factory, monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    from worker.tasks import run_image_candidate

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await _add_character_image(db_session_factory, pid)
    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        shot.motion_prompt = "walk forward"
        await s.commit()
    cid = await _seed_candidate(db_session_factory, pid)

    with patch("app.services.image_generation.generate_tail_frame", new=AsyncMock(side_effect=_fake_gen())) as gtf, \
         patch("worker.tasks.pick_first_frame", new=AsyncMock(return_value=None)):
        await run_image_candidate(worker_ctx, pid, 1, cid, "user:test")

    gtf.assert_awaited_once()
    assert gtf.await_args.kwargs["motion_prompt"] == "walk forward"
    async with db_session_factory() as s:
        cand = (await s.execute(select(ImageCandidate).where(ImageCandidate.id == cid))).scalar_one()
        assert cand.status == "done"
        assert "candidates" in cand.file_path
        assert Path(cand.file_path).read_bytes() == b"GEN"


async def test_custom_prompt_routes_to_generate_custom(worker_ctx, db_session_factory, monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    from worker.tasks import run_image_candidate

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await _add_character_image(db_session_factory, pid)
    cid = await _seed_candidate(
        db_session_factory, pid, slot="first_frame", custom_prompt="my prompt",
        ref_paths=json.dumps({"character": [], "object": []}),
    )

    with patch("app.services.image_generation.generate_custom", new=AsyncMock(side_effect=_fake_gen())) as gc:
        await run_image_candidate(worker_ctx, pid, 1, cid, "user:test")

    gc.assert_awaited_once()
    kw = gc.await_args.kwargs
    assert kw["prompt"] == "my prompt"
    assert kw["character_ref_paths"] == []   # 显式空列表不回退默认


async def test_cc_slot_routes_to_calibrate_face(worker_ctx, db_session_factory, monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    from worker.tasks import run_image_candidate
    from app.services.storage import shot_dir

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await _add_character_image(db_session_factory, pid)
    s_dir = shot_dir(pid, 1); s_dir.mkdir(parents=True, exist_ok=True)
    lf = s_dir / "last_frame_1_aaaa.png"; lf.write_bytes(b"LF")
    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        shot.last_frame_path = str(lf)
        await s.commit()
    cid = await _seed_candidate(db_session_factory, pid, slot="cc")

    with patch("app.services.image_generation.calibrate_face", new=AsyncMock(side_effect=_fake_gen(b"CC"))) as cf:
        await run_image_candidate(worker_ctx, pid, 1, cid, "user:test")

    cf.assert_awaited_once()
    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        cand = (await s.execute(select(ImageCandidate).where(ImageCandidate.id == cid))).scalar_one()
        assert cand.status == "done"
        assert shot.last_frame_path == str(lf)  # CC 候选化：不直写 last_frame


async def test_failure_marks_candidate_failed_only(worker_ctx, db_session_factory, monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    from worker.tasks import run_image_candidate
    from app.models.project import Project

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await _add_character_image(db_session_factory, pid)
    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        shot.motion_prompt = "m"
        await s.commit()
    cid = await _seed_candidate(db_session_factory, pid)

    with patch("app.services.image_generation.generate_tail_frame", new=AsyncMock(side_effect=RuntimeError("blocked"))), \
         patch("worker.tasks.pick_first_frame", new=AsyncMock(return_value=None)):
        await run_image_candidate(worker_ctx, pid, 1, cid, "user:test")

    async with db_session_factory() as s:
        cand = (await s.execute(select(ImageCandidate).where(ImageCandidate.id == cid))).scalar_one()
        proj = (await s.execute(select(Project).where(Project.id == pid))).scalar_one()
        assert cand.status == "failed" and "blocked" in cand.error
        assert proj.status == "shot_review"  # project 状态机不受影响
