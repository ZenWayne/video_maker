import subprocess
from pathlib import Path

import pytest

from app.agents.video_trimmer import get_video_info


@pytest.fixture
def real_video(tmp_path):
    out = tmp_path / "v.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=30",
         "-frames:v", "48", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(out)],
        check=True, capture_output=True,
    )
    return out


def test_get_video_info_gives_fps_and_frames(real_video):
    info = get_video_info(str(real_video))
    assert round(info["fps"]) == 30
    assert info["total_frames"] == 48
