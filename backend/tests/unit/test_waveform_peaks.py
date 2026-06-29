"""Unit tests for extract_waveform_peaks.

合成视频用 ffmpeg lavfi (sine 440Hz + apad/静音); 无 ffmpeg 时跳过。
"""

import shutil
import pytest
from pathlib import Path

from ffmpeg import FFmpeg

from app.agents.video_trimmer import extract_waveform_peaks

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg binary not found in PATH",
)


def _make_video_with_audio(path: Path, total: float = 2.0) -> None:
    """全程 440Hz 正弦音频 + 纯色视频。"""
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


def _make_video_no_audio(path: Path, total: float = 2.0) -> None:
    """纯视频,无音轨 (-an)。"""
    (
        FFmpeg()
        .option("y")
        .input(f"color=blue:size=64x64:rate=24:duration={total}", f="lavfi")
        .output(
            str(path),
            pix_fmt="yuv420p",
            vcodec="libx264",
            an=None,
        )
    ).execute()


def _make_video_sine_then_silence(path: Path, speech: float = 1.0, total: float = 2.0) -> None:
    """前 speech 秒 440Hz 正弦，之后 apad 补静音到 total 秒。"""
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_audio_video_returns_correct_length(tmp_path):
    """有音频的视频应返回长度 == buckets (默认 200) 的列表。"""
    video = tmp_path / "audio.mp4"
    _make_video_with_audio(video, total=2.0)
    peaks = extract_waveform_peaks(str(video))
    assert isinstance(peaks, list)
    assert len(peaks) == 200


def test_audio_video_has_nonzero_peaks(tmp_path):
    """440Hz 正弦音频 → 至少一半的桶峰值应 > 0.1。"""
    video = tmp_path / "audio.mp4"
    _make_video_with_audio(video, total=2.0)
    peaks = extract_waveform_peaks(str(video))
    nonzero = sum(1 for p in peaks if p > 0.1)
    assert nonzero > len(peaks) // 2, f"Too few loud buckets: {nonzero}/200"


def test_audio_video_values_in_range(tmp_path):
    """所有峰值应在 [0, 1] 范围内。"""
    video = tmp_path / "audio.mp4"
    _make_video_with_audio(video, total=2.0)
    peaks = extract_waveform_peaks(str(video))
    assert all(0.0 <= p <= 1.0 for p in peaks), "Peak out of [0,1] range"


def test_no_audio_returns_empty(tmp_path):
    """无音轨视频 (-an) 应返回空列表。"""
    video = tmp_path / "noaudio.mp4"
    _make_video_no_audio(video, total=2.0)
    peaks = extract_waveform_peaks(str(video))
    assert peaks == []


def test_custom_buckets(tmp_path):
    """buckets 参数应控制返回列表长度。"""
    video = tmp_path / "audio100.mp4"
    _make_video_with_audio(video, total=2.0)
    peaks = extract_waveform_peaks(str(video), buckets=100)
    assert len(peaks) == 100


def test_sine_then_silence_trailing_buckets_near_zero(tmp_path):
    """前半有声 / 后半静音 → 尾部桶峰值应接近 0，头部桶明显 > 0。"""
    video = tmp_path / "sine_silence.mp4"
    _make_video_sine_then_silence(video, speech=1.0, total=2.0)
    peaks = extract_waveform_peaks(str(video), buckets=200)
    assert len(peaks) == 200
    # 前 60 桶 (前 ~30%) 应有音频
    early = peaks[:60]
    assert any(p > 0.05 for p in early), "Expected audio in early buckets"
    # 尾部 60 桶 (后 ~30%) 应接近静音
    late = peaks[-60:]
    assert all(p < 0.05 for p in late), f"Expected silence in late buckets, got max={max(late):.4f}"
