"""resolve_reference_prompt_wav — pure logic, object_store mocked (no real COS)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.reference_voice import resolve_reference_prompt_wav
from app.services import object_store


@pytest.mark.asyncio
async def test_file_source_returns_existing_key(monkeypatch):
    monkeypatch.setattr(object_store, "exists", AsyncMock(return_value=True))
    proj = SimpleNamespace(
        reference_voice_path="projects/p1/reference_voice/prompt.wav",
        reference_voice_shot_id=None,
    )
    got = await resolve_reference_prompt_wav("p1", proj, session=None)
    assert got == "projects/p1/reference_voice/prompt.wav"


@pytest.mark.asyncio
async def test_file_source_missing_returns_none(monkeypatch):
    monkeypatch.setattr(object_store, "exists", AsyncMock(return_value=False))
    proj = SimpleNamespace(
        reference_voice_path="projects/p1/reference_voice/prompt.wav",
        reference_voice_shot_id=None,
    )
    assert await resolve_reference_prompt_wav("p1", proj, session=None) is None


@pytest.mark.asyncio
async def test_no_source_returns_none():
    proj = SimpleNamespace(reference_voice_path=None, reference_voice_shot_id=None)
    assert await resolve_reference_prompt_wav("p1", proj, session=None) is None


@pytest.mark.asyncio
async def test_run_voice_convert_uses_file_source(monkeypatch):
    """run_voice_convert must pass the resolved key through to _do_voice_convert_one."""
    import worker.tasks as tasks
    from unittest.mock import MagicMock
    import app.services.reference_voice as reference_voice_module

    project = MagicMock(reference_voice_path="projects/p1/reference_voice/prompt.wav",
                        reference_voice_shot_id=None)

    sess = AsyncMock()
    res = MagicMock()
    res.scalar_one_or_none.return_value = project
    sess.execute.return_value = res
    sf = MagicMock()
    sf.return_value.__aenter__.return_value = sess
    sf.return_value.__aexit__.return_value = False

    async def fake_resolver(pid, proj, session):
        return proj.reference_voice_path
    monkeypatch.setattr(reference_voice_module, "resolve_reference_prompt_wav", fake_resolver)

    captured = {}
    async def fake_do_one(session_factory, redis, pid, sid, ref):
        captured["ref"] = ref
    monkeypatch.setattr(tasks, "_do_voice_convert_one", fake_do_one)

    ctx = {"session_factory": sf, "redis": MagicMock()}
    await tasks.run_voice_convert(ctx, "p1", 2, "user:test")
    assert captured["ref"] == "projects/p1/reference_voice/prompt.wav"
