"""VC/CC 链路：备份用服务端 copy，pristine 尾帧不被 CC 覆盖。"""
# 这道护栏只为 adopt_candidate_to_last_frame（app.api.image_candidates）和
# character_calibrate_revert（app.api.pipeline，经 client fixture 走 HTTP）
# 而设，且它模拟的是**真实生产行为**、不掩盖任何 bug：这两者只在 FastAPI
# app 里被调用，生产环境唯一入口就是 uvicorn 先跑 app.main 把全部路由子模块
# 按固定顺序 import 完，所以"先把 app.main 加载完整"就是生产的真实导入顺序。
# 问题只出在测试进程里——pytest 允许某个测试文件先直接
# `from app.api.image_candidates import ...`，绕过 app.main 的顺序 import，
# 撞上"正在加载中"的半成品模块 → AttributeError（同款坑 test_stream_
# snapshot_candidates.py 已踩过一次）。
#
# 反例对照：本文件里 ensure_pre_vc_backup（app.services.vc_backup）的测试
# 完全不依赖这道护栏——它跑在 vc-worker 进程，那个进程从不 import app.main，
# 所以"先加载 app.main 再测"反而会掩盖真实入口的循环 import bug（Task 9
# 审查发现的 Critical 缺陷正是这样被测试掩盖过一次）。该函数的"不依赖
# app.main"结论改为在独立、未加载 app.main 的进程里验证，见 Task 9 报告。
import app.main  # noqa: F401

from sqlalchemy import select

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import _make_project, _add_shot, seed_shot_with_source, HEADERS

from app.models.project import Shot
from app.services import object_store

pytestmark = requires_cos


async def test_cc_adopt_preserves_pristine_last_frame(
    db_session_factory, cos_prefix, tmp_path
):
    """CC 覆盖 last_frame_path，但 pristine_last_frame_key 必须岿然不动。"""
    from app.api.image_candidates import adopt_candidate_to_last_frame
    from app.db import AsyncSession as session_factory

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await seed_shot_with_source(db_session_factory, pid, 1)

    pristine_key = f"projects/{pid}/shots/shot_1/last_frame_orig.png"
    f = tmp_path / "orig.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"o" * 64)
    await object_store.put(pristine_key, f)

    cand_key = f"projects/{pid}/shots/shot_1/candidates/cc_cand.png"
    g = tmp_path / "cand.png"
    g.write_bytes(b"\x89PNG\r\n\x1a\n" + b"c" * 64)
    await object_store.put(cand_key, g)

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.last_frame_path = pristine_key
        shot.pristine_last_frame_key = pristine_key
        await s.commit()

    await adopt_candidate_to_last_frame(db_session_factory, pid, 1, cand_key)

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()

    assert shot.cc_status == "done"
    assert shot.last_frame_path != pristine_key       # 已被校准结果覆盖
    assert shot.pristine_last_frame_key == pristine_key  # 还原目标必须保住
    assert await object_store.exists(pristine_key)


async def test_character_calibrate_revert_uses_pristine_key_and_deletes_old_cc(
    client, db_session_factory, cos_prefix, tmp_path
):
    """character-calibrate-revert 端点：以 pristine_last_frame_key 为唯一还原目标，
    DB 提交后才删旧 cc_ 对象。"""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await seed_shot_with_source(db_session_factory, pid, 1)

    pristine_key = f"projects/{pid}/shots/shot_1/last_frame_pristine.png"
    f = tmp_path / "pristine.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"p" * 64)
    await object_store.put(pristine_key, f)

    cc_key = f"projects/{pid}/shots/shot_1/cc_current.png"
    g = tmp_path / "cc.png"
    g.write_bytes(b"\x89PNG\r\n\x1a\n" + b"c" * 64)
    await object_store.put(cc_key, g)

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.last_frame_path = cc_key
        shot.pristine_last_frame_key = pristine_key
        shot.cc_status = "done"
        await s.commit()

    r = await client.post(
        f"/api/projects/{pid}/shots/1/character-calibrate-revert", headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cc_status"] is None

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()

    assert shot.last_frame_path == pristine_key
    assert shot.cc_status is None
    assert not await object_store.exists(cc_key)          # 旧校准帧已删
    assert await object_store.exists(pristine_key)          # pristine 本体不受影响


async def test_vc_backup_uses_server_side_copy(db_session_factory, cos_prefix):
    """VC 首次执行时备份原视频，用服务端 copy 不产生本地流量。

    ensure_pre_vc_backup 特意放在 app.services.vc_backup（纯 service 模块，
    不依赖 app.main）——vc-worker 进程只 import worker.tasks，从不 import
    app.main；若从 app.api.pipeline 导入会在该进程处理第一个 run_voice_convert
    任务时崩溃（ImportError: 循环 import）。这个测试直接从新位置导入，不依赖
    本文件顶部的 `import app.main` 护栏——它就是用来确认"不先加载 app.main
    也能正常工作"这件事本身。
    """
    from app.services.vc_backup import ensure_pre_vc_backup

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    video_key = await seed_shot_with_source(db_session_factory, pid, 1)

    backup_key = await ensure_pre_vc_backup(db_session_factory, pid, 1)
    assert await object_store.exists(backup_key)
    assert await object_store.size(backup_key) == await object_store.size(video_key)

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
    assert shot.pre_vc_video_key == backup_key

    # 幂等：已备份则返回原备份，不重复拷贝
    again = await ensure_pre_vc_backup(db_session_factory, pid, 1)
    assert again == backup_key


async def test_resolve_tail_frame_used_when_object_exists(cos_prefix, tmp_path):
    """(B) resolve_tail_frame 对真实存在的 COS key 必须返回该 key（不是恒 False）。"""
    from worker.tasks import resolve_tail_frame

    key = f"{cos_prefix}tail.png"
    f = tmp_path / "tail.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"t" * 32)
    await object_store.put(key, f)

    assert await resolve_tail_frame(key) == key


async def test_resolve_tail_frame_none_when_object_missing(cos_prefix):
    from worker.tasks import resolve_tail_frame

    assert await resolve_tail_frame(f"{cos_prefix}missing.png") is None


async def test_vc_convert_publishes_fixed_key_and_backs_up_video(
    db_session_factory, cos_prefix, tmp_path, monkeypatch
):
    """(A) 完整 VC 链路：写固定 audio_vc.wav key，video_path 不动，备份已建立。"""
    from unittest.mock import AsyncMock, MagicMock, patch
    from worker.tasks import _do_voice_convert_one

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    video_key = await seed_shot_with_source(db_session_factory, pid, 1)

    ref_key = f"{cos_prefix}ref_prompt.wav"
    ref_local = tmp_path / "ref.wav"
    ref_local.write_bytes(b"RIFFref")
    await object_store.put(ref_key, ref_local)

    def fake_extract(video_path, out_path):
        from pathlib import Path
        Path(out_path).write_bytes(b"fake-src-audio")

    async def fake_vc(src, ref, out):
        from pathlib import Path
        Path(out).write_bytes(b"RIFFfakewav")
        return out

    with (
        patch("app.agents.audio_extractor.extract_audio_wav", side_effect=fake_extract),
        patch("app.services.cosyvoice_client.voice_convert", new=AsyncMock(side_effect=fake_vc)),
        patch("worker.tasks.publish_event", new=AsyncMock()),
    ):
        await _do_voice_convert_one(db_session_factory, MagicMock(), pid, 1, ref_key)

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()

    assert shot.vc_status == "done"
    assert shot.vc_audio_path.endswith("audio_vc.wav")
    assert await object_store.exists(shot.vc_audio_path)
    assert await object_store.get(shot.vc_audio_path, tmp_path / "got.wav")
    assert (tmp_path / "got.wav").read_bytes() == b"RIFFfakewav"
    # video_path untouched (non-destructive)
    assert shot.video_path == video_key
    # pre-VC backup was created as a side effect
    assert shot.pre_vc_video_key is not None
    assert await object_store.exists(shot.pre_vc_video_key)
