"""build_effective_clip: 前段静音把开头压到 ~0，越过点后保留原声。"""
import shutil
import subprocess
import pytest
from pathlib import Path
from ffmpeg import FFmpeg
from app.agents.effective_clip import build_effective_clip

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not found")


def _make_av(path: Path, total: float = 2.0):
    (FFmpeg().option("y")
     .input(f"color=blue:size=64x64:rate=24:duration={total}", f="lavfi")
     .input(f"sine=frequency=440:duration={total}", f="lavfi")
     .output(str(path), pix_fmt="yuv420p", vcodec="libx264", acodec="aac", shortest=None)
    ).execute()


def _rms_db(path: str, start: float, end: float) -> float:
    """astats mean RMS (dB) over [start,end]."""
    r = subprocess.run(
        ["ffmpeg", "-ss", str(start), "-to", str(end), "-i", path,
         "-af", "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    import re
    vals = [
        -120.0 if m.group(1) == "-inf" else float(m.group(1))
        for m in re.finditer(r"RMS_level=(-?[\d.]+|-inf)", r.stderr)
    ]
    return min(vals) if vals else 0.0


def test_head_mute_silences_front_keeps_rest(tmp_path):
    src = tmp_path / "src.mp4"; _make_av(src, 2.0)
    out = tmp_path / "out.mp4"
    # 24fps，静音前 24 帧 = 前 1.0s
    build_effective_clip(str(src), trim_frames=None, vc_audio_path=None,
                         out_path=str(out), audio_head_mute_frames=24)
    assert out.exists()
    front = _rms_db(str(out), 0.1, 0.8)   # 应接近静音（很低 dB）
    back = _rms_db(str(out), 1.2, 1.8)    # 应有声（相对高）
    assert front < back - 20, f"front={front} back={back}: 前段未被静音"


def test_no_head_mute_is_passthrough_copy(tmp_path):
    src = tmp_path / "src.mp4"; _make_av(src, 1.0)
    out = tmp_path / "out.mp4"
    build_effective_clip(str(src), trim_frames=None, vc_audio_path=None,
                         out_path=str(out), audio_head_mute_frames=None)
    assert out.exists()  # 无任何编辑 → 直接 copy
