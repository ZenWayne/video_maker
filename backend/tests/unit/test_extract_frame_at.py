import hashlib
import subprocess
from pathlib import Path

import pytest

from app.agents.frame_porter import extract_frame_at


@pytest.fixture
def color_video(tmp_path):
    """30 帧、每秒 30fps、每帧纯色按帧号渐变的无损测试视频。"""
    # 容器用 .mkv 而不是 .mp4：FFV1 在 MP4 里不是所有 ffmpeg 构建都支持——
    # 开发机的构建接受，Ubuntu（CI runner）的直接报 exit 234。容器格式与被测
    # 的 extract_frame_at 无关，Matroska 两边都收，所以选它。
    # 不能换成 libx264：这个测试要的就是 ffv1 的无损，有损编码会让「同一帧
    # 抽两次字节一致」这个断言失去意义。
    out = tmp_path / "src.mkv"
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
