"""SSE streaming API routes."""

import asyncio
import json
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from app.db import AsyncSession as session_factory
from app.main import get_redis
from app.models.project import Project, Shot
from app.services.events import subscribe_to_events
from app.services.storage import to_media_url

router = APIRouter()


async def event_generator(
    redis,
    project_id: str,
) -> AsyncGenerator[str, None]:
    """Generate SSE events for a project."""
    # Use session only for the snapshot query, then release it immediately.
    # Holding a DB session open for the full SSE stream lifetime exhausts
    # SQLite's connection pool when many streams are open concurrently.
    async with session_factory() as session:
        result = await session.execute(
            select(Project).where(Project.id == project_id)
        )
        project = result.scalar_one_or_none()

        if project is None:
            yield json.dumps({"type": "error", "message": "Project not found"})
            return

        shots_result = await session.execute(
            select(Shot).where(Shot.project_id == project_id).order_by(Shot.shot_id)
        )
        shots = shots_result.scalars().all()

        snapshot = {
            "type": "state_snapshot",
            "data": {
                "project": {
                    "id": project.id,
                    "title": project.title,
                    "theme_text": project.theme_text,
                    "creator_name": project.creator_name,
                    "status": project.status,
                    "scene_overview": project.scene_overview,
                    "final_video_path": project.final_video_path,
                    "error_message": project.error_message,
                    "created_at": str(project.created_at),
                    "updated_at": str(project.updated_at),
                },
                "shots": [
                    {
                        "id": s.id,
                        "shot_id": s.shot_id,
                        "project_id": s.project_id,
                        "text": s.text,
                        "shot_type": s.shot_type,
                        "visual_description": s.visual_description,
                        "shot_duration": s.shot_duration,
                        "status": s.status,
                        "align_with_previous": s.align_with_previous,
                        "motion_prompt": s.motion_prompt,
                        "first_frame_path": to_media_url(s.first_frame_path),
                        "video_path": to_media_url(s.video_path),
                        "last_frame_path": to_media_url(s.last_frame_path),
                        "word_count_warning": s.word_count_warning,
                        "error_message": s.error_message,
                        "custom_first_frame_path": to_media_url(s.custom_first_frame_path),
                        "custom_reference_paths": (
                            [to_media_url(p) for p in json.loads(s.custom_reference_paths)]
                            if s.custom_reference_paths else None
                        ),
                        "reference_image_hint": s.reference_image_hint,
                    }
                    for s in shots
                ],
            },
        }

    # Session released — now yield snapshot and stream Redis events without holding DB connection
    yield json.dumps(snapshot)

    try:
        async for event in subscribe_to_events(redis, project_id):
            yield json.dumps(event)
    except asyncio.CancelledError:
        raise


@router.get("/projects/{project_id}/stream")
async def stream_events(
    project_id: str,
    redis=Depends(get_redis),
):
    """SSE stream for project events."""
    async with session_factory() as session:
        result = await session.execute(
            select(Project).where(Project.id == project_id)
        )
        project = result.scalar_one_or_none()
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

    from sse_starlette.sse import EventSourceResponse
    return EventSourceResponse(
        event_generator(redis, project_id),
        media_type="text/event-stream",
    )
