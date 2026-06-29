"""Tests for build_effective_clip / effective_clip_paths (Task 9)."""

import hashlib
import subprocess
from pathlib import Path

import pytest

from app.agents.effective_clip import build_effective_clip
from app.agents.frame_porter import extract_frame_at
from app.agents.video_trimmer import get_video_info


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


@pytest.fixture
def lossless_src(tmp_path):
    out = tmp_path / "src.mkv"   # mkv container with ffv1
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
            "-frames:v", "120", "-c:v", "ffv1", "-c:a", "pcm_s16le", "-shortest",
            str(out),
        ],
        check=True, capture_output=True,
    )
    return out


def test_trim_only_frame_count(lossless_src, tmp_path):
    out = tmp_path / "clip.mkv"
    build_effective_clip(
        str(lossless_src),
        trim_frames=60,
        vc_audio_path=None,
        out_path=str(out),
        vcodec="ffv1",
        acodec="pcm_s16le",
    )
    assert get_video_info(str(out))["total_frames"] == 60


def test_trim_last_frame_md5_matches_source(lossless_src, tmp_path):
    """Core: baked clip's last frame == source frame 59 (lossless → strict md5)."""
    out = tmp_path / "clip.mkv"
    build_effective_clip(
        str(lossless_src),
        trim_frames=60,
        vc_audio_path=None,
        out_path=str(out),
        vcodec="ffv1",
        acodec="pcm_s16le",
    )
    clip_last = tmp_path / "clip_last.png"
    src_n_minus_1 = tmp_path / "src59.png"
    extract_frame_at(str(out), 59, str(clip_last))
    extract_frame_at(str(lossless_src), 59, str(src_n_minus_1))
    assert _md5(clip_last) == _md5(src_n_minus_1)


def test_no_edit_passthrough(lossless_src, tmp_path):
    """No edits: build copies source bytes directly; frame count unchanged."""
    out = tmp_path / "clip.mkv"
    build_effective_clip(
        str(lossless_src),
        trim_frames=None,
        vc_audio_path=None,
        out_path=str(out),
        vcodec="ffv1",
        acodec="pcm_s16le",
    )
    assert get_video_info(str(out))["total_frames"] == 120
