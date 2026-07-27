"""生成链路产出物落在 COS，且 DB 存的是 key 而非本地路径。

不调用真实模型（计费）——直接把本地 ffmpeg 合成的 mp4 字节喂给
``publish_generated_video``，不经过 generate_video/Veo。
"""
import subprocess
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import _make_project, _add_shot

from app.models.project import ProjectStatus, Shot, ShotStatus
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


async def test_run_shot_pipeline_completes_and_publishes_to_cos(
    db_session_factory, redis, tmp_path, cos_prefix, monkeypatch,
):
    """端到端跑真正的 run_shot_pipeline（不是直接调 publish_generated_video），
    确认 generate_video -> auto_trim -> publish_generated_video -> DB 整条链路
    真的能跑通到 COMPLETED，而不是在 get_video_info 上因假字节 ffprobe 失败被
    吞掉、标记 FAILED 却被测试忽略（此前 test_custom_first_frame_priority.py
    的假象就是这么产生的：只断言 mock_gen.call_args，从不检查最终状态）。

    同时把 shot 对象本身的 video_path/last_frame_path/pristine_last_frame_key
    透传给 propagate_first_frame_to_next 之前 spy 一次，直接验证
    publish_generated_video 返回后、在 run_shot_pipeline 仍在跑的同一个
    session 里，外层 shot 对象是否被正确回填（而不是留着生成前的旧值/None，
    只是因为 expire_on_commit=False 和"目前没人读它"两个巧合才没炸）。
    """
    from worker import tasks as tasks_module

    pid = await _make_project(db_session_factory, status=ProjectStatus.SHOT_GENERATING.value)
    await _add_shot(db_session_factory, pid, 1, status="pending")

    # motion_prompt 已存在 → 走 director 复用分支，跳过真实 LLM 调用；
    # auto_trim=False → 跳过尾帧对齐/语音裁剪分支，聚焦本次要测的发布链路。
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.motion_prompt = "Slow push-in."
        shot.auto_trim = False
        await s.commit()

    fake_video = _make_mp4(tmp_path / "gen.mp4")
    video_bytes = fake_video.read_bytes()

    fake_provider = MagicMock()
    fake_provider.client = None
    monkeypatch.setattr(tasks_module, "get_provider", lambda: fake_provider)
    # 首帧解析不在本 task 范围（first_frame.py），直接跳过，聚焦生成/发布链路。
    monkeypatch.setattr(tasks_module, "pick_first_frame", AsyncMock(return_value=None))
    # 唯一被 mock 的是"计费模型调用"本身——返回真实 ffmpeg 合成的 mp4 字节，
    # 而不是 b"fake-video-bytes"：假字节过不了 ffprobe，会被 pipeline 自己的
    # except 吞掉、标记 FAILED，制造"测试是绿的但实际从未发布到 COS"的假象。
    mock_generate_video = AsyncMock(return_value=video_bytes)
    monkeypatch.setattr(tasks_module, "generate_video", mock_generate_video)

    captured = {}
    real_propagate = tasks_module.propagate_first_frame_to_next

    async def _spy_propagate(project_id, shot_arg, last_frame_path, session):
        # 在 run_shot_pipeline 仍持有的同一个 shot ORM 实例上取值——这是
        # publish_generated_video 返回后、最终 COMPLETED 提交前的那一刻。
        captured["video_path"] = shot_arg.video_path
        captured["last_frame_path"] = shot_arg.last_frame_path
        captured["pristine_last_frame_key"] = shot_arg.pristine_last_frame_key
        return await real_propagate(project_id, shot_arg, last_frame_path, session)

    monkeypatch.setattr(tasks_module, "propagate_first_frame_to_next", _spy_propagate)

    ctx = {"session_factory": db_session_factory, "redis": redis}
    await tasks_module.run_shot_pipeline(ctx, pid, "user:tester", shot_id=1)

    assert mock_generate_video.called, "generate_video 从未被调用"

    # 外层 shot 对象在 propagate_first_frame_to_next 被调用时就已经回填好了
    # 三个字段——不是 None，也不是发布前的旧值。
    assert captured["video_path"] is not None, (
        "外层 shot.video_path 在发布之后仍是 None——publish_generated_video "
        "用了独立 session 写 DB，但没有回填到 run_shot_pipeline 自己持有的 "
        "shot 对象上，未来任何读 shot.video_path 的新代码都会静默拿到旧值"
    )
    assert captured["video_path"].startswith(f"projects/{pid}/shots/shot_1/output_")
    assert captured["last_frame_path"].startswith(f"projects/{pid}/shots/shot_1/last_frame_")
    assert captured["pristine_last_frame_key"] == captured["last_frame_path"]

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()

    # 分镜必须真的跑通到 COMPLETED——不能因为 get_video_info 在假字节上炸掉
    # 而被 except 吞掉标记 FAILED（那样这条测试就又是自欺欺人的绿）。
    assert shot.status == ShotStatus.COMPLETED.value, (
        f"shot ended in status={shot.status!r} (error={shot.error_message!r}); "
        "expected the pipeline to complete for real, not swallow an exception"
    )

    assert shot.video_path == captured["video_path"]
    assert not shot.video_path.startswith("/")
    assert await object_store.exists(shot.video_path)

    assert shot.last_frame_path == captured["last_frame_path"]
    assert await object_store.exists(shot.last_frame_path)

    assert shot.pristine_last_frame_key == shot.last_frame_path
