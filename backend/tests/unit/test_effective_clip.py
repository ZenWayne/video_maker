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


def test_vc_only_audio_bounded_by_video_duration(tmp_path):
    """VC without trim: substituted audio longer than source is clamped by -shortest."""
    # Source: 60 frames @ 30 fps = 2.0 s video
    src = tmp_path / "src.mkv"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-frames:v", "60", "-c:v", "ffv1", "-c:a", "pcm_s16le", "-shortest",
            str(src),
        ],
        check=True, capture_output=True,
    )
    # Replacement audio: 4 s — intentionally longer than the 2 s source video
    vc_wav = tmp_path / "vc.wav"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=4",
            "-c:a", "pcm_s16le",
            str(vc_wav),
        ],
        check=True, capture_output=True,
    )
    out = tmp_path / "vc_clip.mkv"
    build_effective_clip(
        str(src),
        trim_frames=None,
        vc_audio_path=str(vc_wav),
        out_path=str(out),
        vcodec="ffv1",
        acodec="pcm_s16le",
    )
    info = get_video_info(str(out))
    # -shortest must clamp output to ~2.0 s (source video duration), not 4 s
    assert info["duration"] < 2.15, (
        f"Output duration {info['duration']:.3f}s exceeds video duration — -shortest not applied"
    )
    # Video frames must be preserved (no trim)
    assert info["total_frames"] == 60


# ── effective_clip_paths: missing vc_audio_path falls back gracefully ──────────


def test_effective_clip_paths_missing_vc_audio_falls_back(tmp_path, monkeypatch):
    """If vc_audio_path is set but the file does not exist, effective_clip_paths must
    fall back to source audio (pass vc_audio_path=None) without raising an error."""
    import subprocess
    from app.agents.effective_clip import effective_clip_paths
    from app.agents.video_trimmer import get_video_info
    from app import agents  # noqa: F401 — ensure module loaded

    # Build a tiny real source video
    src = tmp_path / f"output_test.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-frames:v", "60", "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-c:a", "aac", "-shortest",
            str(src),
        ],
        check=True, capture_output=True,
    )

    # Monkeypatch shot_source_path so effective_clip_paths finds our fake source
    import app.agents.effective_clip as ec_mod
    monkeypatch.setattr(ec_mod, "shot_source_path", lambda pid, sid: src)

    class FakeShot:
        project_id = "proj1"
        shot_id = 1
        video_path = str(src)  # DB source-of-truth path (the immutable source)
        trim_frames = None
        vc_audio_path = str(tmp_path / "nonexistent_vc.wav")  # does NOT exist

    out_dir = str(tmp_path / "out")
    import os; os.makedirs(out_dir, exist_ok=True)

    # Must not raise; must return exactly one path (passthrough to source)
    result = effective_clip_paths([FakeShot()], out_dir)
    assert len(result) == 1
    # No baked clip created — falls back to passthrough (no trim, no vc)
    assert result[0] == str(src)
    # Verify output is a valid video
    info = get_video_info(result[0])
    assert info["total_frames"] == 60


def test_trim_cuts_audio_to_video_duration(tmp_path):
    """Regression: trimming must cut the AUDIO to the video duration, not leave a
    full-length (uncut) audio track (which made the stitched preview too long)."""
    import subprocess
    from app.agents.effective_clip import build_effective_clip

    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440",
         "-frames:v", "120", "-pix_fmt", "yuv420p",
         "-c:v", "libx264", "-c:a", "aac", "-shortest", str(src)],
        check=True, capture_output=True,
    )
    out = tmp_path / "clip.mp4"
    build_effective_clip(str(src), trim_frames=60, vc_audio_path=None, out_path=str(out))

    def stream_dur(stream: str) -> float:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", stream,
             "-show_entries", "stream=duration", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True,
        )
        return float(r.stdout.strip())

    vdur, adur = stream_dur("v:0"), stream_dur("a:0")
    # video ≈ 60/30 = 2.0s; audio must be cut to ~the same, not the full ~4.0s
    assert abs(adur - vdur) < 0.2, f"audio {adur}s not aligned to video {vdur}s"
    assert adur < 2.6, f"audio not cut — {adur}s (full source was ~4s)"
