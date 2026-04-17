"""HTTP client for CosyVoice Voice Conversion service."""

import logging
from pathlib import Path

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def voice_convert(source_wav: str, prompt_wav: str, output_wav: str) -> str:
    """Call the CosyVoice VC service to convert voice timbre.

    Args:
        source_wav: Path to source audio (voice to be converted)
        prompt_wav: Path to reference audio (target voice timbre)
        output_wav: Path to save converted audio

    Returns:
        The output_wav path

    Raises:
        httpx.HTTPStatusError: If the VC service returns an error
    """
    Path(output_wav).parent.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=120.0) as client:
        with open(source_wav, "rb") as src, open(prompt_wav, "rb") as ref:
            logger.info(
                "Calling CosyVoice VC: source=%s, prompt=%s", source_wav, prompt_wav
            )
            resp = await client.post(
                f"{settings.cosyvoice_url}/vc",
                files={
                    "source_audio": ("source.wav", src, "audio/wav"),
                    "prompt_audio": ("prompt.wav", ref, "audio/wav"),
                },
            )
            resp.raise_for_status()
            Path(output_wav).write_bytes(resp.content)

    logger.info("VC result saved to %s", output_wav)
    return output_wav
