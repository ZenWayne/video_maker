"""In-process voice conversion using vc2.VoiceConverter (no HTTP)."""

import asyncio
import logging
import os
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)


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
