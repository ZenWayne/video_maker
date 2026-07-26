"""Integration test: VC worker publishes a fixed audio_vc.wav key; source video untouched."""
# _do_voice_convert_one lazily imports app.api.pipeline.ensure_pre_vc_backup at call
# time; app.main's routers form the same partial-circular-import trap documented in
# test_stream_snapshot_candidates.py, so force app.main to finish loading first.
import app.main  # noqa: F401

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from worker.tasks import _do_voice_convert_one
from tests.integration.conftest import _make_project, _add_shot, seed_shot_with_source, HEADERS
from tests.integration.conftest_cos import requires_cos
from app.services import object_store

pytestmark = requires_cos


async def test_vc_writes_wav_only_keeps_source_and_backs_up(
    db_session_factory, cos_prefix, tmp_path
):
    """VC worker must publish a fixed audio_vc.wav key and leave the source video
    byte-for-byte untouched; a pre-VC backup is created as a side effect."""
    from pathlib import Path

    pid = await _make_project(db_session_factory, status="completed")
    await _add_shot(db_session_factory, pid, 1)
    video_key = await seed_shot_with_source(db_session_factory, pid, 1)

    before = tmp_path / "before.mp4"
    await object_store.get(video_key, before)
    before_bytes = before.read_bytes()

    ref_key = f"{cos_prefix}ref.wav"
    ref_local = tmp_path / "ref.wav"
    ref_local.write_bytes(b"RIFFref")
    await object_store.put(ref_key, ref_local)

    def fake_extract(video_path, out_path):
        Path(out_path).write_bytes(b"fake-audio-src")

    async def fake_vc(src, ref, out):
        Path(out).write_bytes(b"RIFFfakewav")

    with (
        patch("app.agents.audio_extractor.extract_audio_wav", side_effect=fake_extract),
        patch("app.services.cosyvoice_client.voice_convert", new=AsyncMock(side_effect=fake_vc)),
        patch("worker.tasks.publish_event", new=AsyncMock()),
    ):
        await _do_voice_convert_one(db_session_factory, MagicMock(), pid, 1, ref_key)

    from sqlalchemy import select
    from app.models.project import Shot

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()

        assert shot.vc_status == "done"
        assert shot.vc_audio_path is not None
        assert shot.vc_audio_path.endswith("audio_vc.wav")
        assert await object_store.exists(shot.vc_audio_path)
        got = tmp_path / "got.wav"
        await object_store.get(shot.vc_audio_path, got)
        assert got.read_bytes() == b"RIFFfakewav"
        assert shot.video_path == video_key   # video_path unchanged (non-destructive)
        # pre-VC backup created as a side effect, idempotent server-side copy
        assert shot.pre_vc_video_key is not None
        assert await object_store.exists(shot.pre_vc_video_key)

    # Source video bytes are identical
    after = tmp_path / "after.mp4"
    await object_store.get(video_key, after)
    assert after.read_bytes() == before_bytes


async def test_voice_revert_clears_audio(client, db_session_factory, cos_prefix, tmp_path):
    """voice-revert must delete the vc_audio object, clear vc metadata, leave source untouched."""
    from sqlalchemy import select
    from app.models.project import Shot
    from app.services.storage import shot_audio_vc_key

    pid = await _make_project(db_session_factory, status="completed")
    await _add_shot(db_session_factory, pid, 1)
    video_key = await seed_shot_with_source(db_session_factory, pid, 1)

    before = tmp_path / "before.mp4"
    await object_store.get(video_key, before)
    before_bytes = before.read_bytes()

    # Publish a real (fake) vc audio object at the fixed key
    vc_key = shot_audio_vc_key(pid, 1)
    wav_local = tmp_path / "vc.wav"
    wav_local.write_bytes(b"RIFFfakewav")
    await object_store.put(vc_key, wav_local)

    # Set shot to vc-done state
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.vc_status = "done"
        shot.vc_audio_path = vc_key
        await s.commit()

    r = await client.post(
        f"/api/projects/{pid}/shots/1/voice-revert",
        headers=HEADERS,
    )

    assert r.status_code == 200
    data = r.json()
    assert data["vc_status"] is None
    assert data["vc_audio_url"] is None
    assert not await object_store.exists(vc_key)

    after = tmp_path / "after.mp4"
    await object_store.get(video_key, after)
    assert after.read_bytes() == before_bytes
