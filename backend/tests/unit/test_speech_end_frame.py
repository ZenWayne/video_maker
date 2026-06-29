"""Unit tests for speech_end_frame helper.

合成视频用 ffmpeg lavfi(sine 人声 + apad 尾部静音);无 ffmpeg 时跳过。
"""

import shutil
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
