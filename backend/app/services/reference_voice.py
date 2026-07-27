"""Resolve and normalize the project base voice for CosyVoice voice conversion."""
import subprocess
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Shot
from app.services import object_store
from app.services.storage import reference_voice_prompt_key, shot_audio_original_key
from app.services.workspace import workspace


def has_audio_stream(input_path: str) -> bool:
    """True if the file has at least one audio stream."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", input_path],
        check=True, capture_output=True, text=True,
    ).stdout
    return "audio" in out


def normalize_reference_voice(input_path: str, out_wav: str) -> str:
    """Extract/transcode any mp4/m4a/wav into a mono 16kHz wav for the CosyVoice prompt."""
    Path(out_wav).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-vn", "-ac", "1", "-ar", "16000",
         "-f", "wav", out_wav],
        check=True, capture_output=True,
    )
    return out_wav


async def resolve_reference_prompt_wav(
    project_id: str, project, session: AsyncSession
) -> Optional[str]:
    """The single source of truth for which prompt wav VC should use.

    Returns a COS **key** (not a local path) — all *_path fields on
    Project/Shot now hold COS keys, so "does it exist" must be decided
    against the object store, never the local filesystem (that was exactly
    the class of bug this migration exists to eliminate).

    Uploaded file wins (mutual exclusivity guarantees only one is set). For a
    shot source, lazily extract audio_original.wav from the reference shot's
    (immutable) video and cache it in COS under a fixed key so repeat VC runs
    against the same reference shot don't re-extract every time.
    """
    if project.reference_voice_path:
        key = project.reference_voice_path
        return key if await object_store.exists(key) else None

    if project.reference_voice_shot_id:
        ref_sid = project.reference_voice_shot_id
        ref_audio_key = shot_audio_original_key(project_id, ref_sid)
        if not await object_store.exists(ref_audio_key):
            result = await session.execute(
                select(Shot).where(
                    Shot.project_id == project_id, Shot.shot_id == ref_sid
                )
            )
            ref_shot = result.scalar_one_or_none()
            if ref_shot is None or not ref_shot.video_path:
                return None
            from app.agents.audio_extractor import extract_audio_wav

            async with workspace() as ws:
                local_video = await ws.fetch(ref_shot.video_path, name="ref_video.mp4")
                local_audio = ws.path("audio_original.wav")
                extract_audio_wav(str(local_video), str(local_audio))
                await ws.publish(local_audio, ref_audio_key)
        return ref_audio_key

    return None


__all__ = [
    "has_audio_stream",
    "normalize_reference_voice",
    "resolve_reference_prompt_wav",
    "reference_voice_prompt_key",
]
