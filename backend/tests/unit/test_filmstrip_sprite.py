"""filmstrip sprite: 一次 ffmpeg 产出 count×1 横向缩略图条。内容级断言 sprite 尺寸。"""
import shutil
import subprocess
from pathlib import Path

import pytest

from app.agents.video_trimmer import extract_filmstrip_sprite

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not in PATH")


def _dims(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    w, h = out.stdout.strip().split(",")
    return int(w), int(h)


def _make_src(path: Path, frames: int = 120) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=128x72:rate=24",
         "-frames:v", str(frames), "-pix_fmt", "yuv420p", "-c:v", "libx264", str(path)],
        check=True, capture_output=True,
    )


def test_sprite_is_count_cells_wide(tmp_path):
    src = tmp_path / "src.mp4"
    out = tmp_path / "strip.png"
    _make_src(src)

    n = extract_filmstrip_sprite(str(src), str(out), count=12, cell_width=96)

    assert n == 12
    assert out.exists()
    w, h = _dims(out)
    # 12 cells × 96px wide, 16:9 cell → 54px tall
    assert w == pytest.approx(12 * 96, abs=12)
    assert h == pytest.approx(54, abs=4)
