"""锁住核心不变量：源帧 N-1 == 分镜 last_frame == 烤片末帧（无损链路严格 md5）。"""
import hashlib
import subprocess
from pathlib import Path

import pytest

from app.agents.effective_clip import build_effective_clip
from app.agents.frame_porter import extract_frame_at


def _md5(p): return hashlib.md5(Path(p).read_bytes()).hexdigest()


@pytest.fixture
def src(tmp_path):
    out = tmp_path / "out.mkv"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
            "-frames:v", "120", "-c:v", "ffv1", "-c:a", "pcm_s16le", "-shortest",
            str(out),
        ],
        check=True, capture_output=True)
    return out


@pytest.mark.parametrize("n", [30, 60, 90])
def test_trim_last_frame_equals_source_frame(src, tmp_path, n):
    # 模拟 trim 端点的抽帧逻辑：源第 n-1 帧
    lf = tmp_path / f"lf_{n}.png"
    extract_frame_at(str(src), n - 1, str(lf))
    # effective clip 末帧（无损）
    clip = tmp_path / f"clip_{n}.mkv"
    build_effective_clip(str(src), trim_frames=n, vc_audio_path=None,
                         out_path=str(clip), vcodec="ffv1", acodec="pcm_s16le")
    clip_last = tmp_path / f"cl_{n}.png"
    extract_frame_at(str(clip), n - 1, str(clip_last))
    assert _md5(lf) == _md5(clip_last)


def test_build_does_not_modify_source(src, tmp_path):
    before = _md5(src)
    clip = tmp_path / "c.mkv"
    build_effective_clip(str(src), trim_frames=60, vc_audio_path=None,
                         out_path=str(clip), vcodec="ffv1", acodec="pcm_s16le")
    assert _md5(src) == before
