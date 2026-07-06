"""Regression test: state_snapshot SSE event must serialize image_candidates
(which carry raw datetime created_at/adopted_at) without raising TypeError.

See app/api/stream.py — json.dumps(snapshot, default=str), matching the
project-wide convention in app/services/events.py:35.
"""
import json

from sqlalchemy import select

from tests.integration.conftest import _make_project, _add_shot
from app.models.project import ImageCandidate, Shot


async def test_state_snapshot_serializes_image_candidates(client, db_session_factory, redis):
    """The first state_snapshot event must be valid JSON and include the
    seeded image candidate, even though ImageCandidate.created_at/adopted_at
    are raw datetime objects on the ORM model.

    `client` fixture is unused directly but must be requested first: it fully
    imports app.main (and all routers) before we import app.api.stream below,
    avoiding a partially-initialized-module circular import (see
    test_stream_snapshot_uses_media_urls in test_pipeline.py for precedent).
    """
    from app.api.stream import event_generator

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)

    async with db_session_factory() as s:
        shot = (
            await s.execute(select(Shot).where(Shot.project_id == pid))
        ).scalar_one()
        s.add(
            ImageCandidate(
                project_id=pid,
                shot_pk=shot.id,
                shot_id=1,
                slot="tail_frame",
                status="done",
                file_path=None,
            )
        )
        await s.commit()

    gen = event_generator(redis, pid)
    # Prior to the fix, json.dumps(snapshot) (no default=str) raises TypeError
    # here because ImageCandidate.created_at is a raw datetime.
    first_event_json = await gen.__anext__()
    await gen.aclose()

    event = json.loads(first_event_json)
    assert event["type"] == "state_snapshot"
    shots = event["data"]["shots"]
    assert len(shots) == 1
    candidates = shots[0]["image_candidates"]
    assert len(candidates) == 1
    assert candidates[0]["slot"] == "tail_frame"
    assert candidates[0]["status"] == "done"
    # created_at must have been coerced to a string by default=str
    assert isinstance(candidates[0]["created_at"], str)
