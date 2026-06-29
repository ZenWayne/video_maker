"""Integration test: VC worker produces only audio_vc wav; source video untouched."""
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from worker.tasks import _do_voice_convert_one
from tests.integration.conftest import _make_project, _add_shot, seed_shot_with_source


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_vc_writes_wav_only_keeps_source(db_session_factory, monkeypatch, tmp_path):
    """VC worker must write audio_vc_<ts>_<uuid>.wav and leave source video untouched."""
    from app.config import settings
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))

    pid = await _make_project(db_session_factory, status="completed")
    await _add_shot(db_session_factory, pid, 1)
    source_mp4 = await seed_shot_with_source(db_session_factory, pid, 1)
    before_md5 = _md5(source_mp4)

    def fake_extract(video_path, out_path):
        Path(out_path).write_bytes(b"fake-audio-src")

    async def fake_vc(src, ref, out):
        Path(out).write_bytes(b"RIFFfakewav")

    with (
        patch("app.agents.audio_extractor.extract_audio_wav", side_effect=fake_extract),
        patch("app.services.cosyvoice_client.voice_convert", new=AsyncMock(side_effect=fake_vc)),
        patch("worker.tasks.publish_event", new=AsyncMock()),
    ):
        await _do_voice_convert_one(db_session_factory, MagicMock(), pid, 1, "/tmp/ref.wav")

    from sqlalchemy import select
    from app.models.project import Shot

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()

        assert shot.vc_status == "done"
        assert shot.vc_audio_path is not None
        assert Path(shot.vc_audio_path).name.startswith("audio_vc_")
        assert Path(shot.vc_audio_path).exists()
        assert shot.video_path == str(source_mp4)   # video_path unchanged

    # Source video bytes are identical
    assert _md5(source_mp4) == before_md5

    # No vc_*.mp4 files created
    shot_directory = source_mp4.parent
    assert not list(shot_directory.glob("vc_*.mp4"))

    # Temp audio_in_*.wav cleaned up
    assert not list(shot_directory.glob("audio_in_*.wav"))
