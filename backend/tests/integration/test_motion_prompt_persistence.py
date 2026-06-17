"""Regression test: motion_prompt must persist after a shot's video is generated.

Bug: in run_shot_pipeline the director branch set ``shot.motion_prompt`` in
memory and then called ``session.refresh(shot)`` (to pick up late-uploaded
reference images). With ``autoflush=False`` the refresh re-loaded the row from
the DB and discarded the uncommitted motion_prompt, so completed shots ended up
with ``motion_prompt = NULL`` and the frontend hid the "运镜提示词" edit button.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from sqlalchemy import select

from app.models.project import Project, Shot, ProjectStatus, ShotStatus
from app.config import settings
import worker.tasks as tasks

DIRECTOR_PROMPT = "Slow push-in to a tight close-up."


@pytest.mark.asyncio
async def test_motion_prompt_persisted_after_video_generation(
    db_session_factory, redis, tmp_path, monkeypatch
):
    # Storage writes (output.mp4 / last_frame.png) go under tmp_path.
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))

    # Arrange: a project mid-generation with one pending shot (director branch).
    async with db_session_factory() as s:
        project = Project(
            id="proj-mp",
            title="t",
            theme_text="theme",
            creator_name="tester",
            status=ProjectStatus.SHOT_GENERATING.value,
            aspect_ratio="9:16",
        )
        s.add(project)
        s.add(Shot(
            project_id="proj-mp",
            shot_id=1,
            text="hello world",
            shot_type="Close-up",
            visual_description="a face",
            shot_duration=4,
            status=ShotStatus.PENDING.value,
            align_with_previous=False,
            auto_trim=False,
        ))
        await s.commit()

    # Mock only the paid / external calls; everything else runs for real.
    fake_provider = MagicMock()
    fake_provider.client = None
    monkeypatch.setattr(tasks, "get_provider", lambda: fake_provider)
    monkeypatch.setattr(tasks, "run_director_agent", AsyncMock(return_value=DIRECTOR_PROMPT))
    monkeypatch.setattr(tasks, "generate_video", AsyncMock(return_value=b"fake-mp4-bytes"))
    monkeypatch.setattr(tasks, "_pick_first_frame", AsyncMock(return_value=None))
    monkeypatch.setattr(tasks, "extract_last_frame", MagicMock(return_value=None))

    ctx = {"session_factory": db_session_factory, "redis": redis}

    # Act
    await tasks.run_shot_pipeline(ctx, "proj-mp", "user:tester")

    # Assert: shot completed AND its motion_prompt survived to the DB.
    async with db_session_factory() as s:
        shot = (
            await s.execute(select(Shot).where(Shot.shot_id == 1, Shot.project_id == "proj-mp"))
        ).scalar_one()
        assert shot.status == ShotStatus.COMPLETED.value
        assert shot.video_path is not None
        assert shot.motion_prompt == DIRECTOR_PROMPT
