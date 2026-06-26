# backend/tests/unit/test_auto_vc.py
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
import worker.auto_vc as auto_vc


class _FakeArq:
    def __init__(self, *a, **k):
        self.calls = []
        _FakeArq.last = self
    async def enqueue_job(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _redis():
    return MagicMock(connection_pool=MagicMock())


def _session():
    s = AsyncMock()
    s.add = MagicMock()
    return s


@pytest.fixture(autouse=True)
def patch_arq(monkeypatch):
    monkeypatch.setattr(auto_vc, "ArqRedis", _FakeArq)


@pytest.fixture(autouse=True)
def patch_resolver(monkeypatch, tmp_path):
    wav = tmp_path / "prompt.wav"
    wav.write_bytes(b"x")
    monkeypatch.setattr(auto_vc, "resolve_reference_prompt_wav",
                        lambda pid, proj: wav if proj.reference_voice_path else None)


async def test_enqueues_when_enabled_and_file_source():
    proj = SimpleNamespace(auto_voice_calibrate=True, reference_voice_path="/x.wav",
                           reference_voice_shot_id=None)
    shot = SimpleNamespace(shot_id=3, vc_status=None)
    sess = _session()
    assert await auto_vc.maybe_enqueue_auto_vc(_redis(), sess, "p1", proj, shot) is True
    args, kwargs = _FakeArq.last.calls[0]
    assert args == ("run_voice_convert", "p1", 3, "system:auto-vc")
    assert kwargs["_queue_name"] == "arq:vc"
    assert shot.vc_status == "converting"


async def test_skips_when_disabled():
    proj = SimpleNamespace(auto_voice_calibrate=False, reference_voice_path="/x.wav",
                           reference_voice_shot_id=None)
    shot = SimpleNamespace(shot_id=3, vc_status=None)
    assert await auto_vc.maybe_enqueue_auto_vc(_redis(), _session(), "p1", proj, shot) is False


async def test_skips_reference_shot_itself():
    proj = SimpleNamespace(auto_voice_calibrate=True, reference_voice_path=None,
                           reference_voice_shot_id=3)
    shot = SimpleNamespace(shot_id=3, vc_status=None)
    # shot source resolver returns a path (proj.reference_voice_path falsy → None here),
    # so disable file branch and assert skip on identity
    assert await auto_vc.maybe_enqueue_auto_vc(_redis(), _session(), "p1", proj, shot) is False


async def test_skips_when_already_converting():
    proj = SimpleNamespace(auto_voice_calibrate=True, reference_voice_path="/x.wav",
                           reference_voice_shot_id=None)
    shot = SimpleNamespace(shot_id=3, vc_status="done")
    assert await auto_vc.maybe_enqueue_auto_vc(_redis(), _session(), "p1", proj, shot) is False
