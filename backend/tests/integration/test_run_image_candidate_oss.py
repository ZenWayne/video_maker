"""run_image_candidate worker（真实 COS）：三个 slot（first_frame / tail_frame /
cc）+ custom_prompt 路由 + 失败路径。只 mock app.services.image_generation 的
生成函数（避免计费），生成函数的输入/输出全部走真实 COS + workspace。

背景（Task 11b，此前无人认领）：worker/tasks.py 的 run_image_candidate 在 slot
分支之前无条件引用已删除的 shot_candidates_dir，导致三个 slot 全部必然崩溃；
经 app/api/image_candidates.py 的 create_image_candidate 端点可达——每次创建
候选图都会入队一个必崩的任务。旧的 tests/integration/test_run_image_candidate.py
用本地 shot_dir()/settings.storage_root 播种，COS 下已过时，替换为本文件。
"""
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from unittest.mock import AsyncMock, patch

from tests.integration.conftest import _make_project, _add_shot
from tests.integration.conftest_cos import requires_cos
from app.models.project import ImageCandidate, Project, Shot
from app.services import object_store
from app.services.storage import shot_candidates_prefix

pytestmark = requires_cos


@pytest.fixture
async def worker_ctx(db_session_factory, redis):
    return {"session_factory": db_session_factory, "redis": redis}


async def _seed_char_ref(tmp_path, pid, content=b"CHAR_REF"):
    key = f"projects/{pid}/reference_images/char_ref.jpg"
    tmp_path.mkdir(parents=True, exist_ok=True)
    f = tmp_path / "ref.jpg"
    f.write_bytes(content)
    await object_store.put(key, f)
    return key


async def _seed_shot_video_key(tmp_path, pid, sid, name, content=b"SRC"):
    key = f"projects/{pid}/shots/shot_{sid}/{name}"
    tmp_path.mkdir(parents=True, exist_ok=True)
    f = tmp_path / name
    f.write_bytes(content)
    await object_store.put(key, f)
    return key


async def _seed_candidate(sf, pid, shot_id=1, slot="tail_frame", custom_prompt=None, ref_paths=None):
    async with sf() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == shot_id)
        )).scalar_one()
        c = ImageCandidate(
            project_id=pid, shot_pk=shot.id, shot_id=shot_id, slot=slot,
            status="generating",
            custom_prompt=custom_prompt,
            prompt_source="custom" if custom_prompt else "auto",
            ref_paths=ref_paths,
        )
        s.add(c)
        await s.commit()
        await s.refresh(c)
        return c.id


def _fake_gen(out_bytes=b"GEN"):
    """Fakes an app.services.image_generation function: writes bytes to the
    LOCAL output_path it's handed (a workspace path) — never touches COS
    directly, mirroring the real functions' contract."""
    async def _fake(*args, **kwargs):
        out = kwargs.get("output_path") or args[-1]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(out_bytes)
        return out
    return _fake


async def _assert_candidate_done(sf, pid, cid, expected_bytes, tmp_path):
    async with sf() as s:
        cand = (await s.execute(
            select(ImageCandidate).where(ImageCandidate.id == cid)
        )).scalar_one()
    assert cand.status == "done"
    # file_path must be a COS KEY under the shot's candidates prefix, not a
    # local path — this is the whole point of the fix.
    assert cand.file_path.startswith(shot_candidates_prefix(pid, 1))
    assert not cand.file_path.startswith("/")
    assert await object_store.exists(cand.file_path)
    got = tmp_path / "got.png"
    await object_store.get(cand.file_path, got)
    assert got.read_bytes() == expected_bytes
    return cand


async def test_auto_first_frame_slot_publishes_candidate_to_cos(
    worker_ctx, db_session_factory, tmp_path, cos_prefix
):
    from worker.tasks import run_image_candidate

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    char_key = await _seed_char_ref(tmp_path / "a", pid)
    cid = await _seed_candidate(
        db_session_factory, pid, slot="first_frame",
        ref_paths=json.dumps({"character": [char_key], "object": []}),
    )

    captured = {}

    async def _fake_ff(*args, **kwargs):
        # Character ref path handed to the generation function must be a
        # REAL LOCAL FILE with the right bytes (i.e. actually fetched from
        # COS into the workspace) — not the raw COS key string, which
        # Path(key).exists()/read_bytes() would silently treat as "missing"
        # and drop from the prompt with no error.
        captured["char_ref_paths"] = list(kwargs["character_ref_paths"])
        for p in captured["char_ref_paths"]:
            assert Path(p).exists()
            assert Path(p).read_bytes() == b"CHAR_REF"
        out = kwargs["output_path"]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"FF_OUT")
        return out

    with patch("app.services.image_generation.generate_first_frame", new=AsyncMock(side_effect=_fake_ff)) as gff:
        await run_image_candidate(worker_ctx, pid, 1, cid, "user:test")

    gff.assert_awaited_once()
    await _assert_candidate_done(db_session_factory, pid, cid, b"FF_OUT", tmp_path / "b")


async def test_auto_tail_frame_slot_uses_motion_prompt_and_publishes_to_cos(
    worker_ctx, db_session_factory, tmp_path, cos_prefix
):
    from worker.tasks import run_image_candidate

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    char_key = await _seed_char_ref(tmp_path / "a", pid)
    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        shot.motion_prompt = "walk forward"
        await s.commit()
    cid = await _seed_candidate(
        db_session_factory, pid, slot="tail_frame",
        ref_paths=json.dumps({"character": [char_key], "object": []}),
    )

    with patch("app.services.image_generation.generate_tail_frame", new=AsyncMock(side_effect=_fake_gen(b"TF_OUT"))) as gtf, \
         patch("worker.tasks.pick_first_frame", new=AsyncMock(return_value=None)):
        await run_image_candidate(worker_ctx, pid, 1, cid, "user:test")

    gtf.assert_awaited_once()
    assert gtf.await_args.kwargs["motion_prompt"] == "walk forward"
    await _assert_candidate_done(db_session_factory, pid, cid, b"TF_OUT", tmp_path / "b")


async def test_cc_slot_fetches_pristine_and_publishes_candidate(
    worker_ctx, db_session_factory, tmp_path, cos_prefix
):
    from worker.tasks import run_image_candidate

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    char_key = await _seed_char_ref(tmp_path / "a", pid)
    pristine_key = await _seed_shot_video_key(tmp_path / "b", pid, 1, "last_frame_pristine.png", b"PRISTINE")
    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        shot.last_frame_path = pristine_key
        shot.pristine_last_frame_key = pristine_key
        shot.cc_status = "calibrating"
        await s.commit()
    cid = await _seed_candidate(
        db_session_factory, pid, slot="cc",
        ref_paths=json.dumps({"character": [char_key]}),
    )

    captured = {}

    async def _fake_cc(refs, src, out):
        captured["src"] = src
        assert Path(src).exists()
        assert Path(src).read_bytes() == b"PRISTINE"
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"CC_OUT")
        return out

    with patch("app.services.image_generation.calibrate_face", new=AsyncMock(side_effect=_fake_cc)) as cf:
        await run_image_candidate(worker_ctx, pid, 1, cid, "user:test")

    cf.assert_awaited_once()
    await _assert_candidate_done(db_session_factory, pid, cid, b"CC_OUT", tmp_path / "c")
    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        # CC 候选化：不直写 last_frame，cc_status 因不是已采纳 cc_*.png 而清空
        assert shot.last_frame_path == pristine_key
        assert shot.cc_status is None


async def test_custom_prompt_routes_to_generate_custom_with_explicit_empty_refs(
    worker_ctx, db_session_factory, tmp_path, cos_prefix
):
    """显式空 character 列表不应回退到项目默认角色参考图（ref_image_ids 语义）。"""
    from worker.tasks import run_image_candidate

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    cid = await _seed_candidate(
        db_session_factory, pid, slot="first_frame", custom_prompt="my custom prompt",
        ref_paths=json.dumps({"character": [], "object": []}),
    )

    with patch("app.services.image_generation.generate_custom", new=AsyncMock(side_effect=_fake_gen(b"CUSTOM_OUT"))) as gc:
        await run_image_candidate(worker_ctx, pid, 1, cid, "user:test")

    gc.assert_awaited_once()
    kw = gc.await_args.kwargs
    assert kw["prompt"] == "my custom prompt"
    assert kw["character_ref_paths"] == []
    await _assert_candidate_done(db_session_factory, pid, cid, b"CUSTOM_OUT", tmp_path / "b")


async def test_failure_marks_candidate_failed_only(
    worker_ctx, db_session_factory, tmp_path, cos_prefix
):
    from worker.tasks import run_image_candidate

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        shot.motion_prompt = "m"
        await s.commit()
    cid = await _seed_candidate(
        db_session_factory, pid, slot="tail_frame",
        ref_paths=json.dumps({"character": [], "object": []}),
    )

    with patch("app.services.image_generation.generate_tail_frame", new=AsyncMock(side_effect=RuntimeError("blocked"))), \
         patch("worker.tasks.pick_first_frame", new=AsyncMock(return_value=None)):
        await run_image_candidate(worker_ctx, pid, 1, cid, "user:test")

    async with db_session_factory() as s:
        cand = (await s.execute(select(ImageCandidate).where(ImageCandidate.id == cid))).scalar_one()
        proj = (await s.execute(select(Project).where(Project.id == pid))).scalar_one()
        assert cand.status == "failed" and "blocked" in cand.error
        assert proj.status == "shot_review"  # project 状态机不受影响
