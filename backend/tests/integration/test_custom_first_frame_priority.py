"""Regression: the first frame has a SINGLE stored source — custom_first_frame_path.

custom_first_frame_path (the user's explicit 首帧 choice) is the ONLY persisted
first-frame field. The frame fed to the model is resolved fresh every run by
services.first_frame.pick_first_frame (custom → previous shot's last frame →
references). There is no cached "resolved" copy to go stale, so a re-uploaded
首帧 is always honored — even on a shot that already generated once (its stored
motion_prompt sends it down the director-reuse path).

Because resolution validates existence, the seeded frames must EXIST on disk
(that's how the real pipeline works — a first frame is a real file).

Three tests:
  1. Priority: connected shot WITH custom_first_frame_path → generate_video
     called with the custom path (not the previous shot's last frame).
  2. Re-upload: a shot that already generated (motion_prompt set) then had a NEW
     first frame uploaded → regeneration uses the NEW custom frame.
  3. Auto-continuity: connected shot WITHOUT custom_first_frame_path still
     auto-uses the previous shot's last frame.
"""
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.models.project import Project, Shot, ProjectStatus, ShotStatus
import worker.tasks as tasks

PROJECT_ID = "proj-cfp-priority"
MOTION = "Slow push-in."


@pytest.fixture(autouse=True)
def _stub_object_store_exists(monkeypatch):
    """pick_first_frame/get_first_character_ref now ask object_store.exists()
    instead of Path.exists() (COS keys, not local paths, in production). This
    file seeds real local files under tmp_path and only cares about the pure
    resolution/priority LOGIC (custom > prev-last-frame > character-ref,
    re-upload honored, stale-pointer self-heal, user-override never touched)
    — not actual COS I/O — so the object-store existence oracle is stubbed to
    the equivalent local-filesystem check. This mirrors the existing
    ``ws.fetch``/``object_store.get`` boundary stubs in
    tests/unit/test_video_generator.py and tests/unit/test_tail_frame.py: the
    real pick_first_frame/propagate_first_frame_to_next code still runs
    unmodified, only the "does this key exist" dependency is substituted.
    """
    from app.services import object_store

    async def _exists(key):
        return Path(key).exists()

    monkeypatch.setattr(object_store, "exists", _exists)


def _mk_frame(tmp_path, name: str) -> str:
    """Create a real image file on disk and return its path (as the pipeline expects)."""
    p = tmp_path / name
    p.write_bytes(b"img-bytes-" + name.encode())
    return str(p)


async def _seed_db(
    db_session_factory,
    *,
    prev_last_frame: str,
    shot2_custom_first_frame: str | None,
):
    """Create project + shot 1 (completed, has last_frame_path) + shot 2 (pending)."""
    async with db_session_factory() as s:
        s.add(Project(
            id=PROJECT_ID,
            title="t",
            theme_text="theme",
            creator_name="tester",
            status=ProjectStatus.SHOT_GENERATING.value,
            aspect_ratio="9:16",
        ))
        # Shot 1: previous shot with a real last frame on disk
        s.add(Shot(
            project_id=PROJECT_ID,
            shot_id=1,
            text="prev dialogue",
            shot_type="Wide Shot",
            visual_description="wide scene",
            shot_duration=4,
            status=ShotStatus.COMPLETED.value,
            align_with_previous=False,
            use_prev_last_frame=False,
            auto_trim=False,
            last_frame_path=prev_last_frame,
        ))
        # Shot 2: connected shot, pending. motion_prompt is set so the worker
        # reuses the director take (the reuse path); the first frame must still be
        # resolved fresh from custom_first_frame_path / continuity.
        s.add(Shot(
            project_id=PROJECT_ID,
            shot_id=2,
            text="next dialogue",
            shot_type="Close-up",
            visual_description="close scene",
            shot_duration=4,
            status=ShotStatus.PENDING.value,
            align_with_previous=True,
            use_prev_last_frame=True,
            auto_trim=False,
            motion_prompt=MOTION,
            custom_first_frame_path=shot2_custom_first_frame,
        ))
        await s.commit()


def _run_ctx(monkeypatch, tmp_path):
    """Common monkeypatching: mocked provider + billed model call."""
    fake_provider = MagicMock()
    fake_provider.client = None
    monkeypatch.setattr(tasks, "get_provider", lambda: fake_provider)
    mock_gen = AsyncMock(return_value=b"fake-video-bytes")
    monkeypatch.setattr(tasks, "generate_video", mock_gen)
    monkeypatch.setattr(tasks, "extract_last_frame", MagicMock(return_value=None))
    return mock_gen


@pytest.mark.asyncio
async def test_custom_first_frame_path_takes_priority_over_connected_shot_override(
    db_session_factory, redis, tmp_path, monkeypatch
):
    """Connected shot WITH custom_first_frame_path must NOT be overridden by prev last frame."""
    custom = _mk_frame(tmp_path, "custom_first_frame.png")
    prev = _mk_frame(tmp_path, "prev_last_frame.png")
    await _seed_db(db_session_factory, prev_last_frame=prev, shot2_custom_first_frame=custom)
    mock_gen = _run_ctx(monkeypatch, tmp_path)

    ctx = {"session_factory": db_session_factory, "redis": redis}
    await tasks.run_shot_pipeline(ctx, PROJECT_ID, "user:tester", shot_id=2)

    assert mock_gen.called, "generate_video was never called"
    _, kwargs = mock_gen.call_args
    assert kwargs["first_frame_path"] == custom, (
        f"Expected custom first frame {custom!r} but got {kwargs['first_frame_path']!r}. "
        "The connected-shot override is silently discarding the user's custom_first_frame_path."
    )


@pytest.mark.asyncio
async def test_reuploaded_first_frame_is_used_on_regeneration(
    db_session_factory, redis, tmp_path, monkeypatch
):
    """A re-uploaded 首帧 must be used on regeneration of an already-generated shot.

    Real-world bug this guards: the shot already generated once (motion_prompt is
    stored, so it takes the director-reuse path). The user uploads a NEW first
    frame → custom_first_frame_path points at it. Regeneration must resolve the
    first frame fresh and feed the model the NEW frame. There is no stored
    first_frame_path that could keep the old image alive.
    """
    new_frame = _mk_frame(tmp_path, "new_uploaded_first_frame.png")
    prev = _mk_frame(tmp_path, "prev_last_frame.png")
    await _seed_db(db_session_factory, prev_last_frame=prev, shot2_custom_first_frame=new_frame)
    mock_gen = _run_ctx(monkeypatch, tmp_path)

    ctx = {"session_factory": db_session_factory, "redis": redis}
    await tasks.run_shot_pipeline(ctx, PROJECT_ID, "user:tester", shot_id=2)

    assert mock_gen.called, "generate_video was never called"
    _, kwargs = mock_gen.call_args
    assert kwargs["first_frame_path"] == new_frame, (
        f"Expected freshly-uploaded custom frame {new_frame!r} but got "
        f"{kwargs['first_frame_path']!r}. Regeneration is not honoring the re-uploaded 首帧."
    )


@pytest.mark.asyncio
async def test_connected_shot_without_custom_first_frame_uses_prev_last_frame(
    db_session_factory, redis, tmp_path, monkeypatch
):
    """Connected shot WITHOUT custom_first_frame_path still auto-uses prev shot's last frame."""
    prev = _mk_frame(tmp_path, "prev_last_frame.png")
    await _seed_db(db_session_factory, prev_last_frame=prev, shot2_custom_first_frame=None)
    mock_gen = _run_ctx(monkeypatch, tmp_path)

    ctx = {"session_factory": db_session_factory, "redis": redis}
    await tasks.run_shot_pipeline(ctx, PROJECT_ID, "user:tester", shot_id=2)

    assert mock_gen.called, "generate_video was never called"
    _, kwargs = mock_gen.call_args
    assert kwargs["first_frame_path"] == prev, (
        f"Expected prev last frame {prev!r} but got {kwargs['first_frame_path']!r}. "
        "Auto-continuity is broken: connected shot without custom override must use prev last frame."
    )


@pytest.mark.asyncio
async def test_stale_auto_first_frame_pointer_is_healed_on_regeneration(
    db_session_factory, redis, tmp_path, monkeypatch
):
    """自动传播的首帧指针指向已删除文件时：生成用回退帧，且 DB 指针被自愈修正。"""
    prev = _mk_frame(tmp_path, "prev_last_frame.png")
    stale = str(tmp_path / "deleted_last_frame.png")  # never written — missing on disk
    await _seed_db(db_session_factory, prev_last_frame=prev, shot2_custom_first_frame=stale)
    mock_gen = _run_ctx(monkeypatch, tmp_path)

    ctx = {"session_factory": db_session_factory, "redis": redis}
    await tasks.run_shot_pipeline(ctx, PROJECT_ID, "user:tester", shot_id=2)

    _, kwargs = mock_gen.call_args
    assert kwargs["first_frame_path"] == prev, "生成必须回退到上一镜当前末帧"

    from sqlalchemy import select
    from app.models.project import Shot
    async with db_session_factory() as s:
        shot2 = (await s.execute(
            select(Shot).where(Shot.project_id == PROJECT_ID, Shot.shot_id == 2)
        )).scalar_one()
        assert shot2.custom_first_frame_path == prev, "悬空指针应被自愈为实际使用的首帧"


@pytest.mark.asyncio
async def test_stale_custom_frames_override_is_never_touched(
    db_session_factory, redis, tmp_path, monkeypatch
):
    """用户覆盖（custom_frames/ 路径）即使文件丢失也绝不被改写。"""
    prev = _mk_frame(tmp_path, "prev_last_frame.png")
    stale_user = str(tmp_path / "custom_frames" / "user_upload.png")  # missing, but user territory
    await _seed_db(db_session_factory, prev_last_frame=prev, shot2_custom_first_frame=stale_user)
    mock_gen = _run_ctx(monkeypatch, tmp_path)

    ctx = {"session_factory": db_session_factory, "redis": redis}
    await tasks.run_shot_pipeline(ctx, PROJECT_ID, "user:tester", shot_id=2)

    from sqlalchemy import select
    from app.models.project import Shot
    async with db_session_factory() as s:
        shot2 = (await s.execute(
            select(Shot).where(Shot.project_id == PROJECT_ID, Shot.shot_id == 2)
        )).scalar_one()
        assert shot2.custom_first_frame_path == stale_user
