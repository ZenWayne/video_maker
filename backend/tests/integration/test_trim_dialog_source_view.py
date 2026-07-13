"""裁剪弹窗只读端点必须以源片（output_*.mp4）为准，而非 video_path 指向的派生文件。

场景：老 shot / VC 后 shot 的 video_path 指向物理剪过的 vc_*.mp4，
若端点直接 ffprobe/提峰值/静音检测该文件，时间轴只剩剪后长度。
"""
from pathlib import Path

import pytest

import app.agents.video_trimmer as vt
from tests.integration.conftest import HEADERS, _make_project
from app.models.project import Shot


async def _seed_shot_with_derived_video(sf, tmp_path, pid):
    """shot 目录里放 output_*.mp4（源片）+ vc_*.mp4（派生），video_path 指派生文件。"""
    shot_dir = tmp_path / "projects" / pid / "shots" / "shot_1"
    shot_dir.mkdir(parents=True)
    source = shot_dir / "output_1700000000_deadbeef.mp4"
    source.write_bytes(b"src")
    derived = shot_dir / "vc_1700000001_cafebabe.mp4"
    derived.write_bytes(b"vc")
    async with sf() as s:
        s.add(Shot(
            project_id=pid, shot_id=1, text="t", shot_type="Medium Shot",
            visual_description="v", shot_duration=6, status="completed",
            align_with_previous=False, video_path=str(derived),
        ))
        await s.commit()
    return str(source), str(derived)


@pytest.fixture
def probe_calls(monkeypatch):
    """Mock 掉所有 ffmpeg 函数，记录每个函数收到的视频路径。"""
    calls = {}

    def _rec(key, ret):
        def f(path, *args, **kwargs):
            calls[key] = path
            calls[key + "_kwargs"] = kwargs
            return ret
        return f

    monkeypatch.setattr(vt, "get_video_info", _rec("info", {
        # duration 故意设为错误值（容器时长/音频尾巴），验证端点会用
        # total_frames/fps 归一化，而不是直接透传 ffprobe 的 duration
        "fps": 24.0, "total_frames": 144, "duration": 7.5,
    }))
    monkeypatch.setattr(vt, "speech_end_info", _rec("speech", (5.0, 120)))
    monkeypatch.setattr(vt, "extract_waveform_peaks", _rec("peaks", [0.5]))
    monkeypatch.setattr(vt, "suggest_silence_trim", _rec("silence", None))
    return calls


async def test_video_info_probes_source_and_returns_source_url(
    client, db_session_factory, tmp_path, probe_calls
):
    pid = await _make_project(db_session_factory, status="shot_review")
    source, _derived = await _seed_shot_with_derived_video(db_session_factory, tmp_path, pid)

    r = await client.get(f"/api/projects/{pid}/shots/1/video-info")
    assert r.status_code == 200
    assert probe_calls["info"] == source, "ffprobe 必须打在源片上"
    assert probe_calls["speech"] == source, "静音检测必须打在源片上"
    assert r.json()["source_video_url"] is not None
    assert "output_1700000000_deadbeef.mp4" in r.json()["source_video_url"]
    # duration 必须按视频流 (total_frames/fps = 144/24) 归一化，
    # 而不是 mock 里故意设错的容器 duration (7.5)
    assert r.json()["duration"] == pytest.approx(6.0)


async def test_waveform_extracts_from_source(
    client, db_session_factory, tmp_path, probe_calls
):
    pid = await _make_project(db_session_factory, status="shot_review")
    source, _ = await _seed_shot_with_derived_video(db_session_factory, tmp_path, pid)

    r = await client.get(f"/api/projects/{pid}/shots/1/waveform")
    assert r.status_code == 200
    assert probe_calls["peaks"] == source
    # 端点必须把视频流时长 (total_frames/fps = 144/24) 作为 max_seconds
    # 传给 extract_waveform_peaks，桶才能按视频时间轴对齐
    assert probe_calls["peaks_kwargs"]["max_seconds"] == pytest.approx(144 / 24.0)


async def test_detect_silence_probes_source(
    client, db_session_factory, tmp_path, probe_calls
):
    pid = await _make_project(db_session_factory, status="shot_review")
    source, _ = await _seed_shot_with_derived_video(db_session_factory, tmp_path, pid)

    r = await client.post(f"/api/projects/{pid}/shots/1/detect-silence", headers=HEADERS)
    assert r.status_code == 200
    assert probe_calls["silence"] == source


async def test_video_info_falls_back_to_video_path_without_source(
    client, db_session_factory, tmp_path, probe_calls
):
    """无 output_*.mp4（异常/极老数据）时回退 video_path，不 500。"""
    pid = await _make_project(db_session_factory, status="shot_review")
    shot_dir = tmp_path / "projects" / pid / "shots" / "shot_1"
    shot_dir.mkdir(parents=True)
    only = shot_dir / "vc_1700000001_cafebabe.mp4"
    only.write_bytes(b"vc")
    async with db_session_factory() as s:
        s.add(Shot(
            project_id=pid, shot_id=1, text="t", shot_type="Medium Shot",
            visual_description="v", shot_duration=6, status="completed",
            align_with_previous=False, video_path=str(only),
        ))
        await s.commit()

    r = await client.get(f"/api/projects/{pid}/shots/1/video-info")
    assert r.status_code == 200
    assert probe_calls["info"] == str(only)
