import hashlib
import subprocess
from pathlib import Path

import pytest

from app.agents.frame_porter import extract_frame_at


@pytest.fixture
def color_video(tmp_path):
    """30 帧、每秒 30fps、每帧纯色按帧号渐变的无损测试视频。"""
    out = tmp_path / "src.mp4"
    # testsrc2 每帧内容不同（带帧号），ffv1 无损 → 帧字节确定
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=30",
         "-frames:v", "30", "-pix_fmt", "yuv420p", "-c:v", "ffv1", str(out)],
        check=True, capture_output=True,
    )
    return out


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def test_extract_frame_at_is_deterministic(color_video, tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    extract_frame_at(str(color_video), 9, str(a))
    extract_frame_at(str(color_video), 9, str(b))
    assert a.exists() and b.exists()
    assert _md5(a) == _md5(b)


def test_extract_frame_at_different_index_differs(color_video, tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    extract_frame_at(str(color_video), 5, str(a))
    extract_frame_at(str(color_video), 9, str(b))
    assert _md5(a) != _md5(b)
