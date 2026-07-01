"""Regression: the first frame has a SINGLE source of truth (_pick_first_frame).

custom_first_frame_path (the user's explicit 首帧 choice) is authoritative. The
worker must ALWAYS re-resolve the first frame from the authoritative inputs
(custom_first_frame_path → previous shot's last frame → references) and must
NEVER read back shot.first_frame_path as a generation input — first_frame_path is
a derived record of the last run and goes stale the moment the user re-uploads a
first frame.

Because resolution goes through _pick_first_frame, the seeded frames must EXIST
on disk (that's how the real pipeline works — a first frame is a real file).

Three tests:
  1. Priority: connected shot WITH custom_first_frame_path → generate_video
     called with the custom path (not the previous shot's last frame).
  2. Stale guard: after a shot has generated once (first_frame_path points at the
     OLD frame), re-uploading a first frame (custom_first_frame_path = NEW) must
     make regeneration use the NEW frame, not the stale first_frame_path.
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
    shot2_first_frame_path: str | None,
):
    """Create project + shot 1 (completed, has last_frame_path) + shot 2 (pending).

    shot2_first_frame_path is the STALE derived record from a prior run; it is set
    independently of custom_first_frame_path so a test can reproduce the real case
    where the two diverge after a re-upload.
    """
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
        # reuses the director take; the first frame must still be re-resolved.
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
            first_frame_path=shot2_first_frame_path,
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
    await _seed_db(
        db_session_factory,
        prev_last_frame=prev,
        shot2_custom_first_frame=custom,
        shot2_first_frame_path=custom,
    )
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
async def test_reuploaded_custom_first_frame_beats_stale_first_frame_path(
    db_session_factory, redis, tmp_path, monkeypatch
):
    """Re-uploading a first frame must win over the cached (stale) first_frame_path.

    Real-world bug: after a shot has generated once, first_frame_path holds the
    previously-resolved frame. The user then uploads a NEW first frame, which the
    upload endpoint writes to custom_first_frame_path only — first_frame_path is
    left pointing at the OLD image. On regeneration the old fast-path reused the
    stale first_frame_path and fed the model the OLD image, ignoring the upload.
    """
    new_frame = _mk_frame(tmp_path, "new_uploaded_first_frame.png")
    prev = _mk_frame(tmp_path, "prev_last_frame.png")
    # first_frame_path is a stale record of an OLD frame that no longer exists.
    stale = str(tmp_path / "old_stale_first_frame.png")

    await _seed_db(
        db_session_factory,
        prev_last_frame=prev,
        shot2_custom_first_frame=new_frame,
        shot2_first_frame_path=stale,
    )
    mock_gen = _run_ctx(monkeypatch, tmp_path)

    ctx = {"session_factory": db_session_factory, "redis": redis}
    await tasks.run_shot_pipeline(ctx, PROJECT_ID, "user:tester", shot_id=2)

    assert mock_gen.called, "generate_video was never called"
    _, kwargs = mock_gen.call_args
    assert kwargs["first_frame_path"] == new_frame, (
        f"Expected freshly-uploaded custom frame {new_frame!r} but got "
        f"{kwargs['first_frame_path']!r}. The fast-path is reusing the stale "
        "first_frame_path instead of re-resolving custom_first_frame_path."
    )


@pytest.mark.asyncio
async def test_connected_shot_without_custom_first_frame_uses_prev_last_frame(
    db_session_factory, redis, tmp_path, monkeypatch
):
    """Connected shot WITHOUT custom_first_frame_path still auto-uses prev shot's last frame.

    This guards the auto-continuity regression: dropping the stale-input reuse must
    NOT break the default case.
    """
    prev = _mk_frame(tmp_path, "prev_last_frame.png")
    await _seed_db(
        db_session_factory,
        prev_last_frame=prev,
        shot2_custom_first_frame=None,
        shot2_first_frame_path=None,
    )
    mock_gen = _run_ctx(monkeypatch, tmp_path)

    ctx = {"session_factory": db_session_factory, "redis": redis}
    await tasks.run_shot_pipeline(ctx, PROJECT_ID, "user:tester", shot_id=2)

    assert mock_gen.called, "generate_video was never called"
    _, kwargs = mock_gen.call_args
    assert kwargs["first_frame_path"] == prev, (
        f"Expected prev last frame {prev!r} but got {kwargs['first_frame_path']!r}. "
        "Auto-continuity is broken: connected shot without custom override must use prev last frame."
    )
