"""Resolve and normalize the project base voice for CosyVoice voice conversion."""
import subprocess
from pathlib import Path

from app.services.storage import (
    project_dir,
    shot_audio_original_path,
    get_original_video_for_audio,
)

REF_VOICE_SUBDIR = "reference_voice"


def reference_voice_dir(project_id: str) -> Path:
    return project_dir(project_id) / REF_VOICE_SUBDIR


def reference_voice_prompt_path(project_id: str) -> Path:
    return reference_voice_dir(project_id) / "prompt.wav"


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


def resolve_reference_prompt_wav(project_id: str, project) -> Path | None:
    """The single source of truth for which prompt wav VC should use.

    Uploaded file wins (mutual exclusivity guarantees only one is set). For a
    shot source, lazily extract audio_original.wav from the reference shot.
    """
    if project.reference_voice_path:
        p = Path(project.reference_voice_path)
        return p if p.exists() else None
    if project.reference_voice_shot_id:
        ref_sid = project.reference_voice_shot_id
        ref_audio = shot_audio_original_path(project_id, ref_sid)
        if not ref_audio.exists():
            from app.agents.audio_extractor import extract_audio_wav
            ref_video = get_original_video_for_audio(project_id, ref_sid)
            extract_audio_wav(str(ref_video), str(ref_audio))
        return ref_audio
    return None
