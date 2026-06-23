from types import SimpleNamespace
from pathlib import Path
from app.services.reference_voice import resolve_reference_prompt_wav


def test_file_source_returns_existing_path(tmp_path):
    wav = tmp_path / "prompt.wav"
    wav.write_bytes(b"RIFF....")
    proj = SimpleNamespace(reference_voice_path=str(wav), reference_voice_shot_id=None)
    assert resolve_reference_prompt_wav("p1", proj) == wav


def test_file_source_missing_returns_none(tmp_path):
    proj = SimpleNamespace(reference_voice_path=str(tmp_path / "nope.wav"),
                           reference_voice_shot_id=None)
    assert resolve_reference_prompt_wav("p1", proj) is None


def test_no_source_returns_none():
    proj = SimpleNamespace(reference_voice_path=None, reference_voice_shot_id=None)
    assert resolve_reference_prompt_wav("p1", proj) is None


import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_run_voice_convert_uses_file_source(tmp_path, monkeypatch):
    import worker.tasks as tasks
    from app.config import settings
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))

    # Project with an uploaded file base voice
    wav = tmp_path / "ref.wav"
    wav.write_bytes(b"RIFF....")
    project = MagicMock(reference_voice_path=str(wav), reference_voice_shot_id=None)

    sess = AsyncMock()
    res = MagicMock()
    res.scalar_one_or_none.return_value = project
    sess.execute.return_value = res
    sf = MagicMock()
    sf.return_value.__aenter__.return_value = sess
    sf.return_value.__aexit__.return_value = False

    captured = {}
    async def fake_do_one(session_factory, redis, pid, sid, ref):
        captured["ref"] = ref
    monkeypatch.setattr(tasks, "_do_voice_convert_one", fake_do_one)

    ctx = {"session_factory": sf, "redis": MagicMock()}
    await tasks.run_voice_convert(ctx, "p1", 2, "user:test")
    assert captured["ref"] == str(wav)
