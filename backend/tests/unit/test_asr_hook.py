from collections import namedtuple

import pytest

import app.agents.asr as asr_module
from app.agents.asr import (
    slice_hook, transcribe, get_supported_language_codes, HOOK_CUTOFF_SEC,
    _FALLBACK_LANGUAGE_CODES,
)

W = namedtuple("W", ["start", "word"])
Segment = namedtuple("Segment", ["text", "words"])
Info = namedtuple("Info", ["language"])


def test_slice_hook_keeps_words_before_cutoff():
    words = [W(0.0, "wait"), W(1.2, "for"), W(2.9, "it"), W(3.4, "because"), W(5.0, "reasons")]
    assert slice_hook(words) == "wait for it"


def test_slice_hook_handles_none_start_and_whitespace():
    words = [W(None, "x"), W(0.5, "  hi "), W(2.0, "there")]
    assert slice_hook(words) == "hi there"


def test_cutoff_is_three_seconds():
    assert HOOK_CUTOFF_SEC == 3.0


class FakeModel:
    """Stand-in for faster_whisper.WhisperModel. Records call kwargs for assertions."""

    def __init__(self, segments, language="en"):
        self._segments = segments
        self._language = language
        self.calls = []

    def transcribe(self, audio_path, **kwargs):
        self.calls.append({"audio_path": audio_path, **kwargs})
        return self._segments, Info(self._language)


@pytest.fixture(autouse=True)
def reset_model_singleton():
    """Ensure the module-global model cache never leaks between tests."""
    asr_module._model = None
    yield
    asr_module._model = None


def _install_fake_model(monkeypatch, segments, language="en"):
    fake = FakeModel(segments, language=language)
    monkeypatch.setattr(asr_module, "_get_model", lambda: fake)
    return fake


def test_transcribe_passes_vad_and_word_timestamps_and_language(monkeypatch):
    words = [W(0.5, "hello")]
    segments = [Segment(text="hello", words=words)]
    fake = _install_fake_model(monkeypatch, segments)

    transcribe("/tmp/audio.wav", language="en")

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["vad_filter"] is True
    assert call["word_timestamps"] is True
    assert call["language"] == "en"
    assert call["audio_path"] == "/tmp/audio.wav"


def test_transcribe_no_segments_means_no_speech(monkeypatch):
    _install_fake_model(monkeypatch, segments=[], language="en")

    result = transcribe("/tmp/audio.wav")

    assert result.has_speech is False
    assert result.full_transcript == ""
    assert result.hook_text == ""


def test_transcribe_whitespace_only_segments_means_no_speech(monkeypatch):
    """Regression test for Finding 2: segments present but the segment-level text is
    empty/whitespace (has_speech=False via `full`), even though the word list itself
    carries non-empty word tokens (e.g. VAD noise artifacts). hook_text must NOT be
    derived from those words in that case — it must stay empty, consistent with
    has_speech=False. Against the pre-fix code (which always computed
    hook_text=slice_hook(words) regardless of has_speech), this would fail because
    hook_text would come back as "hi" instead of "".
    """
    words = [W(0.5, "hi"), W(1.0, "there")]
    segments = [Segment(text="   ", words=words), Segment(text="", words=[])]
    _install_fake_model(monkeypatch, segments)

    result = transcribe("/tmp/audio.wav")

    assert result.has_speech is False
    assert result.full_transcript == ""
    assert result.hook_text == ""


def test_transcribe_normal_speech_assembles_transcript_and_hook(monkeypatch):
    words = [
        W(0.0, "wait"),
        W(1.2, "for"),
        W(2.9, "it"),
        W(3.4, "because"),
        W(5.0, "reasons"),
    ]
    segments = [Segment(text="wait for it because reasons", words=words)]
    _install_fake_model(monkeypatch, segments, language="en")

    result = transcribe("/tmp/audio.wav")

    assert result.has_speech is True
    assert result.full_transcript == "wait for it because reasons"
    assert result.hook_text == "wait for it"
    assert result.language == "en"


def test_get_model_singleton_caches_instance(monkeypatch):
    created = []

    class DummyWhisperModel:
        def __init__(self, *args, **kwargs):
            created.append(1)

    fake_module = type("module", (), {"WhisperModel": DummyWhisperModel})
    monkeypatch.setitem(
        __import__("sys").modules, "faster_whisper", fake_module
    )

    first = asr_module._get_model()
    second = asr_module._get_model()

    assert first is second
    assert len(created) == 1


def test_fallback_language_codes_includes_yue_excludes_jv():
    """Sanity check on the vendored fallback tuple itself: it must mirror
    faster-whisper's real ``_LANGUAGE_CODES`` — which accepts Cantonese as
    "yue" and Javanese as "jw" (NOT "jv")."""
    assert "yue" in _FALLBACK_LANGUAGE_CODES
    assert "jw" in _FALLBACK_LANGUAGE_CODES
    assert "jv" not in _FALLBACK_LANGUAGE_CODES


def test_get_supported_language_codes_falls_back_when_faster_whisper_missing(monkeypatch):
    """Simulate the API process, which does not install the `asr` dependency
    group: `faster_whisper` (and therefore `faster_whisper.tokenizer`) is not
    importable. Setting a module to None in sys.modules is the standard way
    to force `import` to raise ImportError for it."""
    import sys
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    monkeypatch.setitem(sys.modules, "faster_whisper.tokenizer", None)

    codes = get_supported_language_codes()

    assert codes == frozenset(_FALLBACK_LANGUAGE_CODES)
    assert "yue" in codes
    assert "jv" not in codes


def test_get_supported_language_codes_prefers_live_faster_whisper_set(monkeypatch):
    """When faster_whisper IS importable, its live _LANGUAGE_CODES tuple must
    win over the vendored fallback — e.g. a newer faster-whisper adding a
    language should be picked up without touching this codebase."""
    import sys
    import types

    fake_tokenizer_module = types.ModuleType("faster_whisper.tokenizer")
    fake_tokenizer_module._LANGUAGE_CODES = ("en", "zh", "brand-new-lang")
    fake_faster_whisper_module = types.ModuleType("faster_whisper")
    fake_faster_whisper_module.tokenizer = fake_tokenizer_module

    monkeypatch.setitem(sys.modules, "faster_whisper", fake_faster_whisper_module)
    monkeypatch.setitem(sys.modules, "faster_whisper.tokenizer", fake_tokenizer_module)

    codes = get_supported_language_codes()

    assert codes == frozenset(("en", "zh", "brand-new-lang"))
