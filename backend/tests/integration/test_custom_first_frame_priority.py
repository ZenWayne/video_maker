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
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.models.project import Project, Shot, ProjectStatus, ShotStatus
from app.config import settings
import worker.tasks as tasks

PROJECT_ID = "proj-cfp-priority"
MOTION = "Slow push-in."


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
    """Common monkeypatching: real storage root, mocked provider + billed model call."""
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
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
