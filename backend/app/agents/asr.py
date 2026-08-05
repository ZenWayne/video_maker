"""本地 ASR：faster-whisper-large-v3，内置 Silero VAD + 词级时间戳。"""

import logging
import threading
from dataclasses import dataclass
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

HOOK_CUTOFF_SEC = 3.0


@dataclass
class TranscriptResult:
    has_speech: bool
    full_transcript: str
    hook_text: str
    language: Optional[str]


def slice_hook(words, cutoff: float = HOOK_CUTOFF_SEC) -> str:
    """取所有 start < cutoff 的词，拼成 hook_text（母 FRD FR-3.5）。"""
    kept = [
        w.word.strip()
        for w in words
        if getattr(w, "start", None) is not None and w.start < cutoff
    ]
    return " ".join(t for t in kept if t).strip()


# Vendored fallback for `get_supported_language_codes()` below, used only when
# `faster_whisper` cannot be imported (e.g. in the API process, which does not
# install the `asr` dependency group — see `_get_model()`'s lazy import).
# Mirrors `faster_whisper.tokenizer._LANGUAGE_CODES` as of faster-whisper
# 1.2.1. The live set is always preferred when the import succeeds; keep this
# in sync if that upstream tuple changes.
_FALLBACK_LANGUAGE_CODES = (
    "af", "am", "ar", "as", "az", "ba", "be", "bg", "bn", "bo", "br", "bs",
    "ca", "cs", "cy", "da", "de", "el", "en", "es", "et", "eu", "fa", "fi",
    "fo", "fr", "gl", "gu", "ha", "haw", "he", "hi", "hr", "ht", "hu", "hy",
    "id", "is", "it", "ja", "jw", "ka", "kk", "km", "kn", "ko", "la", "lb",
    "ln", "lo", "lt", "lv", "mg", "mi", "mk", "ml", "mn", "mr", "ms", "mt",
    "my", "ne", "nl", "nn", "no", "oc", "pa", "pl", "ps", "pt", "ro", "ru",
    "sa", "sd", "si", "sk", "sl", "sn", "so", "sq", "sr", "su", "sv", "sw",
    "ta", "te", "tg", "th", "tk", "tl", "tr", "tt", "uk", "ur", "uz", "vi",
    "yi", "yo", "zh", "yue",
)


def get_supported_language_codes() -> frozenset:
    """Language codes accepted by the ASR model's ``language=`` kwarg.

    Prefers faster-whisper's live ``_LANGUAGE_CODES`` tuple (imported lazily,
    same reason as `_get_model()`: only the worker installs the `asr`
    dependency group). Falls back to the vendored copy above when
    faster-whisper isn't importable, so callers (e.g. the API's region_hint
    validation) can still validate without pulling in the heavy ASR deps.
    """
    try:
        from faster_whisper.tokenizer import _LANGUAGE_CODES
        return frozenset(_LANGUAGE_CODES)
    except ImportError:
        return frozenset(_FALLBACK_LANGUAGE_CODES)


_model = None
_model_lock = threading.Lock()


def _get_model():
    """进程内单例，避免逐样本重载 ~3GB 权重。

    双重检查锁：transcribe() 经 asyncio.to_thread 从 ARQ worker（max_jobs=4）并发调用，
    没有锁会导致多个线程同时判断 _model is None 并各自构造一次 WhisperModel
    （~3GB 权重重复加载，最后一次赋值静默覆盖前面的）。
    """
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from faster_whisper import WhisperModel  # 延迟导入：仅 worker 装 asr 组
                logger.info("loading faster-whisper model=%s device=%s compute=%s",
                            settings.asr_model, settings.asr_device, settings.asr_compute_type)
                _model = WhisperModel(
                    settings.asr_model,
                    device=settings.asr_device,
                    compute_type=settings.asr_compute_type,
                )
    return _model


def transcribe(audio_path: str, language: Optional[str] = None) -> TranscriptResult:
    """VAD 过滤 + 词级时间戳；无有效语音段 → has_speech=False。"""
    model = _get_model()
    segments, info = model.transcribe(
        audio_path,
        vad_filter=True,
        word_timestamps=True,
        language=language,
    )
    segments = list(segments)  # generator → list
    if not segments:
        return TranscriptResult(False, "", "", getattr(info, "language", None))

    words = [w for seg in segments for w in (seg.words or [])]
    full = " ".join(seg.text.strip() for seg in segments).strip()
    has_speech = bool(full)
    return TranscriptResult(
        has_speech=has_speech,
        full_transcript=full,
        hook_text=slice_hook(words) if has_speech else "",
        language=getattr(info, "language", None),
    )
