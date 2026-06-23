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
