"""生成链路产出物落在 COS，且 DB 存的是 key 而非本地路径。

不调用真实模型（计费）——直接把本地 ffmpeg 合成的 mp4 字节喂给
``publish_generated_video``，不经过 generate_video/Veo。
"""
import subprocess

from sqlalchemy import select

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import _make_project, _add_shot

from app.models.project import Shot
from app.services import object_store

pytestmark = requires_cos


def _make_mp4(path, frames=30):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440", "-frames:v", str(frames),
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac",
         "-shortest", str(path)],
        check=True, capture_output=True,
    )
    return path


async def test_generated_video_lands_in_cos_and_db_stores_key(
    db_session_factory, tmp_path, cos_prefix,
):
    from worker import tasks as tasks_module

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="pending")

    fake = _make_mp4(tmp_path / "gen.mp4")
    video_bytes = fake.read_bytes()

    video_key, last_frame_key = await tasks_module.publish_generated_video(
        db_session_factory, pid, shot_id=1, video_bytes=video_bytes,
    )

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()

    assert shot.video_path == video_key
    assert shot.video_path.startswith(f"projects/{pid}/shots/shot_1/output_")
    assert not shot.video_path.startswith("/")
    assert await object_store.exists(shot.video_path)

    assert shot.last_frame_path == last_frame_key
    assert shot.last_frame_path.startswith(f"projects/{pid}/shots/shot_1/last_frame_")
    assert await object_store.exists(shot.last_frame_path)

    # pristine 尾帧必须同步记录，否则 CC 还原链路会断
    assert shot.pristine_last_frame_key == shot.last_frame_path
