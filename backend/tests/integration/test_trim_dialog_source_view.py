"""裁剪弹窗只读端点必须以 shot.video_path（源片）为准跑 ffprobe/ffmpeg。

背景：Task 8 起 trim/restore-trim/align-tail-frame 都是纯 metadata 操作——
shot.video_path 指向的源对象在 COS 里永不被改写，VC 只写 vc_audio_path。
所以这几个只读展示端点（video-info/waveform/detect-silence）不再需要在
"源片 vs 派生文件" 之间做任何解析,直接对 shot.video_path 做
workspace().fetch() 即可(见 app/api/pipeline.py 的 _fetch_dialog_source)。

真实 COS + 真实 ffmpeg 合成视频(seed_shot_with_source),不 mock 视频探测函数。
"""
import pytest

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import HEADERS, _make_project, _add_shot, seed_shot_with_source

# 注：不用文件级 pytestmark——只有真正 seed 真实 COS 视频的用例才需要
# @requires_cos + cos_prefix。test_video_info_shot_or_video_not_found 只是一个
# 404 路径检查，不碰 COS，文件级标记会让它在无凭证环境里被误 skip（审查发现的
# 过度 gate 问题，本项目已因此丢过三次无凭证回归覆盖）。


@requires_cos
async def test_video_info_probes_source_and_returns_source_url(
    client, db_session_factory, cos_prefix
):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="completed")
    source_key = await seed_shot_with_source(db_session_factory, pid, 1, frames=48)

    r = await client.get(f"/api/projects/{pid}/shots/1/video-info")
    assert r.status_code == 200
    body = r.json()
    assert body["total_frames"] == 48
    assert body["fps"] == pytest.approx(30.0, abs=0.01)
    # duration must be normalized from total_frames/fps (video stream), not the
    # (possibly longer, audio-tail-including) container duration
    assert body["duration"] == pytest.approx(48 / 30.0, abs=0.05)
    assert body["source_video_url"] is not None
    assert body["source_video_url"].startswith("http")
    assert "/api/media/" not in body["source_video_url"]
    # No trim applied yet — nothing to restore
    assert body["has_backup"] is False


@requires_cos
async def test_video_info_has_backup_true_after_trim(
    client, db_session_factory, cos_prefix
):
    """Restore is possible whenever a trim is currently applied (path-as-truth:
    the source object is never overwritten, so "backup" just means trim_frames
    is set)."""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="completed")
    await seed_shot_with_source(db_session_factory, pid, 1, frames=60)

    r = await client.post(
        f"/api/projects/{pid}/shots/1/trim", json={"end_frame": 40}, headers=HEADERS,
    )
    assert r.status_code == 200

    r = await client.get(f"/api/projects/{pid}/shots/1/video-info")
    assert r.status_code == 200
    assert r.json()["has_backup"] is True


async def test_video_info_shot_or_video_not_found(client, db_session_factory):
    """404 前就返回，从不碰 COS——不需要凭证。"""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="pending")  # no video_path yet
    r = await client.get(f"/api/projects/{pid}/shots/1/video-info")
    assert r.status_code == 404


@requires_cos
async def test_waveform_extracts_from_source(client, db_session_factory, cos_prefix):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="completed")
    await seed_shot_with_source(db_session_factory, pid, 1, frames=48)

    r = await client.get(f"/api/projects/{pid}/shots/1/waveform")
    assert r.status_code == 200
    peaks = r.json()["peaks"]
    assert isinstance(peaks, list)
    assert len(peaks) > 0


@requires_cos
async def test_detect_silence_probes_source(client, db_session_factory, cos_prefix):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="completed")
    await seed_shot_with_source(db_session_factory, pid, 1, frames=48)

    r = await client.post(
        f"/api/projects/{pid}/shots/1/detect-silence", headers=HEADERS
    )
    assert r.status_code == 200
    body = r.json()
    # seed_shot_with_source synthesizes a constant 440Hz tone — no trailing
    # silence to detect — but the fallback branch must still return real
    # ffprobe'd video-info fields rather than erroring.
    assert "has_silence" in body
    if not body["has_silence"]:
        assert body["total_frames"] == 48
