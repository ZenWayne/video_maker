"""Task 10: end-to-end md5 invariant — lossless source → effective clip → merge → last frame.

Note: This test exercises build_effective_clip + merge_shots_with_crossfade directly with
lossless ffv1 fixtures.  It does NOT exercise run_merger (the full ARQ worker path needs
Veo/DB/redis); that path is verified separately via AST-check + integration suite.
"""

import hashlib
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.agents.effective_clip import effective_clip_paths, build_effective_clip
from app.agents.frame_porter import extract_frame_at
from app.agents.merger import merge_shots_with_crossfade


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def test_single_shot_export_last_frame_md5(tmp_path):
    """単镜头导出走 c=copy；用无损烤片 → 最终视频末帧 == 源第 N-1 帧（严格 md5）。"""
    src = tmp_path / "out.mkv"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440", "-frames:v", "120",
         "-c:v", "ffv1", "-c:a", "pcm_s16le", "-shortest", str(src)],
        check=True, capture_output=True,
    )
    # 直接烤一个 trim=60 的无损 effective clip
    clip = tmp_path / "eff.mkv"
    build_effective_clip(str(src), trim_frames=60, vc_audio_path=None,
                         out_path=str(clip), vcodec="ffv1", acodec="pcm_s16le")
    # 单输入 merge 走 c=copy → 字节保真
    final = tmp_path / "final.mkv"
    merge_shots_with_crossfade([str(clip)], str(final), crossfade_duration=0.3)

    f_last = tmp_path / "f.png"
    s59 = tmp_path / "s.png"
    extract_frame_at(str(final), 59, str(f_last))
    extract_frame_at(str(src), 59, str(s59))
    assert _md5(f_last) == _md5(s59)
