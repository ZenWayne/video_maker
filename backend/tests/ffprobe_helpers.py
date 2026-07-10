"""ffprobe / ffmpeg assertions shared by the unit and integration suites.

Deliberately not a conftest fixture: `tests.unit` and `tests.integration` both
import this, and neither should depend on the other's fixtures.
"""

import subprocess
from pathlib import Path


def stream_duration(path, kind: str) -> float:
    """Duration in seconds of the first stream of `kind` ('v' or 'a')."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", f"{kind}:0",
         "-show_entries", "stream=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def audio_params(path) -> tuple[int, int]:
    """(sample_rate, channels) of the first audio stream.

    ffprobe emits these fields in stream-struct order regardless of the order
    they are requested in, so sample_rate always comes first.
    """
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate,channels",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    rate, channels = out.stdout.strip().split(",")
    return int(rate), int(channels)


def decode_errors(path) -> int:
    """Decoder failures over a full decode pass.  Zero for a healthy file."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True,
    )
    return out.stderr.count("Error submitting packet")


def make_vc_wav(path, seconds: float = 5.0) -> None:
    """A CosyVoice-shaped wav: 24 kHz, mono."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi",
         "-i", f"sine=frequency=330:sample_rate=24000:duration={seconds}",
         "-ac", "1", "-c:a", "pcm_s16le", str(path)],
        check=True, capture_output=True,
    )
