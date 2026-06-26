"""Regression: custom_first_frame_path must not be overridden by connected-shot logic.

When a connected shot (use_prev_last_frame=True, shot_id > 1) has a user-set
custom_first_frame_path, the pipeline must use THAT path as the generation
first-frame, NOT silently replace it with the previous shot's last_frame_path.

Spec: path-as-truth — custom_first_frame_path is authoritative.

Two tests:
  1. Priority: connected shot WITH custom_first_frame_path → generate_video
     called with the custom path.
  2. Regression: connected shot WITHOUT custom_first_frame_path still auto-uses
     the previous shot's last frame (auto-continuity must remain intact).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, call

from app.models.project import Project, Shot, ProjectStatus, ShotStatus
from app.config import settings
import worker.tasks as tasks

PROJECT_ID = "proj-cfp-priority"
CUSTOM_FRAME = "/fake/shots/custom_first_frame.png"
PREV_LAST_FRAME = "/fake/shots/prev_last_frame.png"
MOTION = "Slow push-in."


async def _seed_db(db_session_factory, *, shot2_custom_first_frame: str | None):
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
        # Shot 1: previous shot with a last frame
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
            last_frame_path=PREV_LAST_FRAME,
        ))
        # Shot 2: connected shot, pending
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
            # Fast-path: both motion_prompt and first_frame_path already set
            # so _pick_first_frame is skipped; the override block is what we test.
            motion_prompt=MOTION,
            first_frame_path=shot2_custom_first_frame or CUSTOM_FRAME,
            custom_first_frame_path=shot2_custom_first_frame,
        ))
        await s.commit()


@pytest.mark.asyncio
async def test_custom_first_frame_path_takes_priority_over_connected_shot_override(
    db_session_factory, redis, tmp_path, monkeypatch
):
    """Connected shot WITH custom_first_frame_path must NOT be overridden by prev last frame."""
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))

    await _seed_db(db_session_factory, shot2_custom_first_frame=CUSTOM_FRAME)

    fake_provider = MagicMock()
    fake_provider.client = None
    monkeypatch.setattr(tasks, "get_provider", lambda: fake_provider)
    mock_gen = AsyncMock(return_value=b"fake-video-bytes")
    monkeypatch.setattr(tasks, "generate_video", mock_gen)
    monkeypatch.setattr(tasks, "extract_last_frame", MagicMock(return_value=None))

    ctx = {"session_factory": db_session_factory, "redis": redis}

    # Act: process shot 2 specifically
    await tasks.run_shot_pipeline(ctx, PROJECT_ID, "user:tester", shot_id=2)

    # Assert: generate_video was called with the CUSTOM first frame, not prev last frame
    assert mock_gen.called, "generate_video was never called"
    _, kwargs = mock_gen.call_args
    assert kwargs["first_frame_path"] == CUSTOM_FRAME, (
        f"Expected custom first frame {CUSTOM_FRAME!r} but got {kwargs['first_frame_path']!r}. "
        "The connected-shot override is silently discarding the user's custom_first_frame_path."
    )


@pytest.mark.asyncio
async def test_connected_shot_without_custom_first_frame_uses_prev_last_frame(
    db_session_factory, redis, tmp_path, monkeypatch
):
    """Connected shot WITHOUT custom_first_frame_path still auto-uses prev shot's last frame.

    This guards the auto-continuity regression: the gate must NOT break the default case.
    """
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))

    # Seed with no custom_first_frame_path on shot 2
    await _seed_db(db_session_factory, shot2_custom_first_frame=None)

    fake_provider = MagicMock()
    fake_provider.client = None
    monkeypatch.setattr(tasks, "get_provider", lambda: fake_provider)
    mock_gen = AsyncMock(return_value=b"fake-video-bytes")
    monkeypatch.setattr(tasks, "generate_video", mock_gen)
    monkeypatch.setattr(tasks, "extract_last_frame", MagicMock(return_value=None))

    ctx = {"session_factory": db_session_factory, "redis": redis}

    await tasks.run_shot_pipeline(ctx, PROJECT_ID, "user:tester", shot_id=2)

    # Assert: generate_video was called with the PREV last frame (auto-continuity)
    assert mock_gen.called, "generate_video was never called"
    _, kwargs = mock_gen.call_args
    assert kwargs["first_frame_path"] == PREV_LAST_FRAME, (
        f"Expected prev last frame {PREV_LAST_FRAME!r} but got {kwargs['first_frame_path']!r}. "
        "Auto-continuity is broken: connected shot without custom override must use prev last frame."
    )
