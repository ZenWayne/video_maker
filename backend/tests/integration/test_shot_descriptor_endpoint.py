"""Regression: the non-destructive playback descriptor must survive the
ProjectResponse/ShotResponse response_model on the live GET endpoint.

The unit test for `_shot_to_dict` bypasses the Pydantic response_model; this
test exercises the real `GET /api/projects/{id}` so a field missing from
`ShotResponse` (which silently strips it) is caught.

Gated on requires_cos/cos_prefix: seed_shot_with_source does a real COS put.
"""
import pytest
from sqlalchemy import select

from app.models.project import Shot
from app.services.storage import shot_key
from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import HEADERS, _make_project, _add_shot, seed_shot_with_source

pytestmark = requires_cos


@pytest.mark.asyncio
async def test_get_project_exposes_playback_descriptor(client, db_session_factory, cos_prefix):
    pid = await _make_project(db_session_factory, status="completed")
    await _add_shot(db_session_factory, pid, 1, status="completed")
    await seed_shot_with_source(db_session_factory, pid, 1, frames=120)

    # Apply an EDL edit: trim + VC pointer. vc_audio_path just needs to be a
    # well-formed COS key — to_media_url() signs it locally (no existence
    # check, no network call), so it doesn't need to actually exist in COS.
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.trim_frames = 60
        shot.vc_audio_path = shot_key(pid, 1, "audio_vc_1_ab.wav")
        await s.commit()

    r = await client.get(f"/api/projects/{pid}", headers=HEADERS)
    assert r.status_code == 200
    shot = r.json()["shots"][0]

    # These keys must reach the client (not stripped by the response_model)
    for key in ("trim_frames", "source_fps", "source_frames", "trim_end_sec", "vc_audio_url"):
        assert key in shot, f"{key} missing from API response (ShotResponse schema gap)"
    assert shot["trim_frames"] == 60
    assert shot["trim_end_sec"] == pytest.approx(60 / shot["source_fps"])
    assert shot["vc_audio_url"] is not None
