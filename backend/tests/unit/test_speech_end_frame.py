"""Unit tests for speech_end_frame helper.

合成视频用 ffmpeg lavfi(sine 人声 + apad 尾部静音);无 ffmpeg 时跳过。
"""

import shutil
import subprocess
import pytest
from pathlib import Path

from ffmpeg import FFmpeg

from app.agents.video_trimmer import speech_end_info, get_video_info

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg binary not found in PATH",
)


def _make_video_trailing_silence(path: Path, speech: float = 1.5, total: float = 2.5) -> None:
    """前 `speech` 秒 440Hz 正弦,之后 apad 补静音直到 `total` 秒。"""
    (
        FFmpeg()
        .option("y")
        .input(f"color=blue:size=64x64:rate=24:duration={total}", f="lavfi")
        .input(f"sine=frequency=440:duration={speech}", f="lavfi")
        .output(
            str(path),
            t=total,
            af="apad",
            pix_fmt="yuv420p",
            vcodec="libx264",
            acodec="aac",
        )
    ).execute()


def _make_video_full_speech(path: Path, total: float = 2.0) -> None:
    """全程正弦,无尾部静音。"""
    (
        FFmpeg()
        .option("y")
        .input(f"color=blue:size=64x64:rate=24:duration={total}", f="lavfi")
        .input(f"sine=frequency=440:duration={total}", f="lavfi")
        .output(
            str(path),
            pix_fmt="yuv420p",
            vcodec="libx264",
            acodec="aac",
            shortest=None,
        )
    ).execute()


def test_returns_frame_near_speech_end(tmp_path):
    video = tmp_path / "trailing.mp4"
    _make_video_trailing_silence(video, speech=1.5, total=2.5)
    fps = get_video_info(str(video))["fps"]

    sec, frame = speech_end_info(str(video), fps)

    assert sec is not None
    assert frame is not None
    # 说话约在 1.5s 结束,24fps → ~36 帧,给静音检测留 ±0.4s 容差
    assert 26 <= frame <= 46


def test_returns_none_when_no_trailing_silence(tmp_path):
    video = tmp_path / "full.mp4"
    _make_video_full_speech(video, total=2.0)
    fps = get_video_info(str(video))["fps"]

    assert speech_end_info(str(video), fps) == (None, None)


def _make_video_mid_silence(path: Path, total: float = 2.5) -> None:
    """人声(1s) → 中间静音(0.5s) → 人声延续到结尾(1s);无尾部静音。
    使用 subprocess 直接调用 ffmpeg 以支持 concat filter_complex。
    """
    speech_dur = 1.0
    silence_dur = 0.5
    # total ≈ speech_dur + silence_dur + speech_dur = 2.5
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=blue:size=64x64:rate=24:duration={total}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={speech_dur}",
        "-f", "lavfi", "-i", f"aevalsrc=0:d={silence_dur}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={speech_dur}",
        "-filter_complex", "[1][2][3]concat=n=3:v=0:a=1[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-t", str(total),
        "-pix_fmt", "yuv420p", "-vcodec", "libx264", "-acodec", "aac",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def test_returns_none_for_mid_silence(tmp_path):
    video = tmp_path / "mid.mp4"
    _make_video_mid_silence(video, total=2.5)
    fps = get_video_info(str(video))["fps"]
    # 中间有 ~0.5s 静音,但人声延续到结尾 → 非尾部静音,不应误判
    assert speech_end_info(str(video), fps) == (None, None)
