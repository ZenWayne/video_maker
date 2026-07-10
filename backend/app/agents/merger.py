"""Merger Agent - concatenates shot videos using ffmpeg."""

import logging
import subprocess
from pathlib import Path

from ffmpeg import FFmpeg

logger = logging.getLogger(__name__)

# The canonical audio format for every re-encoded clip and every merged video.
# ffmpeg's concat demuxer writes ONE audio decoder config for all segments, so
# clips whose rate/layout differ decode as garbage.  Pinning both ends here is
# what keeps that from happening.
CANONICAL_SAMPLE_RATE = 48000
CANONICAL_CHANNELS = 2


def _has_audio(path: str) -> bool:
    """True when the file carries at least one audio stream."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return bool(out.stdout.strip())


def merge_shots(
    shot_paths: list[str],
    output_path: str,
    *,
    vcodec: str = "libx264",
    preset: str = "fast",
    crf: int = 18,
) -> None:
    """Concatenate shot videos into one, hard-cutting between clips.

    Uses ffmpeg's concat *filter*, which builds a decoder per input and
    auto-resamples.  The concat *demuxer* configures a single decoder for every
    segment, so a clip whose audio rate/layout differs from the first one (a
    voice-cloned shot, say) decodes as garbage -- and because the <video>
    element's clock is driven by audio, playback freezes at the segment
    boundary.  The demuxer corrupts silently even when re-encoding, so the
    filter is the only safe option here.

    A single input is stream-copied, which keeps it byte-exact.

    Args:
        shot_paths: Video file paths to concatenate, in order.
        output_path: Path for the merged video.

    Raises:
        ValueError: shot_paths is empty, holds no usable path, or one of the
            inputs carries no audio stream.
        RuntimeError: ffmpeg failed.
    """
    if not shot_paths:
        raise ValueError("No shot paths provided")

    valid_paths = [p for p in shot_paths if p]
    if not valid_paths:
        raise ValueError("No valid shot paths provided")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if len(valid_paths) == 1:
        (
            FFmpeg()
            .option("y")
            .input(valid_paths[0])
            .output(output_path, c="copy", movflags="+faststart")
        ).execute()
        logger.info("Copied single shot to %s", output_path)
        return

    silent = [p for p in valid_paths if not _has_audio(p)]
    if silent:
        raise ValueError(f"Cannot concat: no audio stream in {silent[0]}")

    n = len(valid_paths)
    streams = "".join(f"[{i}:v][{i}:a]" for i in range(n))
    filter_complex = f"{streams}concat=n={n}:v=1:a=1[v][a]"

    cmd = ["ffmpeg", "-y"]
    for p in valid_paths:
        cmd += ["-i", str(Path(p).resolve())]
    cmd += ["-filter_complex", filter_complex, "-map", "[v]", "-map", "[a]",
            "-c:v", vcodec]
    if vcodec == "libx264":
        cmd += ["-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]
    cmd += [
        "-c:a", "aac",
        "-ar", str(CANONICAL_SAMPLE_RATE),
        "-ac", str(CANONICAL_CHANNELS),
        # moov atom at the front → browsers can start playback before the full
        # file downloads (non-faststart mp4 stalls / won't start streaming).
        "-movflags", "+faststart",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("Concat merge failed: %s", result.stderr[-500:])
        raise RuntimeError(f"ffmpeg concat failed: {result.stderr[-300:]}")

    logger.info("Merged %d shots to %s", n, output_path)
