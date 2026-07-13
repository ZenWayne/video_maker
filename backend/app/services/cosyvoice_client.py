"""In-process voice conversion using vc2.VoiceConverter (no HTTP)."""

import asyncio
import logging
import os
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)


def _patch_torchaudio_io() -> None:
    """Route torchaudio.load/save through soundfile.

    torchaudio >= 2.8 removed its built-in decoders: torchaudio.load now
    unconditionally calls load_with_torchcodec (ignoring the `backend` arg),
    which raises ImportError unless the separate `torchcodec` package is
    installed. vc2 calls torchaudio.load/save for its wav I/O, so VC breaks.
    soundfile (cffi over the already-present libsndfile) reads/writes wav
    directly, is version-agnostic, and needs no torchcodec. We replace the
    module attributes vc2 looks up at call time.
    """
    try:
        import numpy as np
        import soundfile as sf
        import torch
        import torchaudio
    except Exception as e:  # soundfile absent / import failure → leave native
        logger.warning("torchaudio soundfile patch skipped: %s", e)
        return

    def _sf_load(filepath, *args, **kwargs):
        # torchaudio.load convention: returns (waveform[channels, frames], sr)
        data, sr = sf.read(str(filepath), dtype="float32", always_2d=True)
        return torch.from_numpy(data.T).contiguous(), sr

    def _sf_save(filepath, src, sample_rate, *args, **kwargs):
        arr = src.detach().cpu().numpy() if hasattr(src, "detach") else np.asarray(src)
        if arr.ndim == 1:
            arr = arr[None, :]
        sf.write(str(filepath), arr.T, int(sample_rate))  # (channels, frames) → (frames, channels)

    torchaudio.load = _sf_load
    torchaudio.save = _sf_save
    logger.info("Patched torchaudio.load/save to use soundfile (torchcodec-free)")


_patch_torchaudio_io()


@lru_cache(maxsize=1)
def _get_converter():
    """Load VoiceConverter once and cache for the process lifetime."""
    from vc2 import VoiceConverter  # installed from CosyVoice/vc2_pkg

    model_dir = os.environ.get("MODEL_DIR", "/workspace/exported_vc2")
    num_threads = int(os.environ.get("VC_NUM_THREADS", "4"))
    logger.info("Loading VoiceConverter from %s (threads=%d)", model_dir, num_threads)
    vc = VoiceConverter(model_dir, num_threads=num_threads)
    logger.info("VoiceConverter ready (sample_rate=%d)", vc.sample_rate)
    return vc


async def voice_convert(source_wav: str, prompt_wav: str, output_wav: str) -> str:
    """Convert voice timbre of source_wav to match prompt_wav.

    Runs onnxruntime inference in a thread-pool executor so the
    async event loop is not blocked.
    """
    Path(output_wav).parent.mkdir(parents=True, exist_ok=True)
    vc = _get_converter()
    loop = asyncio.get_running_loop()
    logger.info("VC: %s → %s", source_wav, output_wav)
    await loop.run_in_executor(None, vc.convert, source_wav, prompt_wav, output_wav)
    logger.info("VC done: %s", output_wav)
    return output_wav
