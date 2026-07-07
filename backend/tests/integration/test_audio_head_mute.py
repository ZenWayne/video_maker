"""前段静音 EDL 字段：落库 + 序列化出 audio_head_mute_sec。"""
import pytest
from sqlalchemy import select
from tests.integration.conftest import HEADERS, _make_project
from app.models.project import Shot


async def test_audio_head_mute_serialized(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    async with db_session_factory() as s:
        s.add(Shot(
            project_id=pid, shot_id=1, text="t", shot_type="Medium Shot",
            visual_description="v", shot_duration=6, status="completed",
            align_with_previous=False, source_fps=24.0, audio_head_mute_frames=12,
        ))
        await s.commit()

    r = await client.get(f"/api/projects/{pid}")
    assert r.status_code == 200
    shot = r.json()["shots"][0]
    assert shot["audio_head_mute_frames"] == 12
    assert shot["audio_head_mute_sec"] == pytest.approx(0.5)  # 12/24


async def test_audio_head_mute_sec_null_without_fps(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    async with db_session_factory() as s:
        s.add(Shot(
            project_id=pid, shot_id=1, text="t", shot_type="Medium Shot",
            visual_description="v", shot_duration=6, status="completed",
            align_with_previous=False, source_fps=None, audio_head_mute_frames=None,
        ))
        await s.commit()

    shot = (await client.get(f"/api/projects/{pid}")).json()["shots"][0]
    assert shot["audio_head_mute_frames"] is None
    assert shot["audio_head_mute_sec"] is None
