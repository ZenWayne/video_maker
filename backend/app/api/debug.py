"""Dev-only debug endpoints for testing.

These routes are only registered in development (never in production builds).
They allow tests to inject SSE events via Redis without triggering real AI calls.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Any, Dict

from app.main import get_redis
from app.services.events import publish_event

router = APIRouter()


class PublishEventRequest(BaseModel):
    event: Dict[str, Any]


@router.post("/projects/{project_id}/debug/publish-event", status_code=202)
async def publish_project_event(
    project_id: str,
    body: PublishEventRequest,
    redis=Depends(get_redis),
):
    """Publish an arbitrary event to a project's Redis channel.

    Used by integration tests to inject SSE events without calling real AI APIs.
    The event flows through the real Redis → SSE path so format bugs are caught.
    """
    ok = await publish_event(redis, project_id, body.event)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to publish event")
    return {"published": True}
