"""Merger Agent - concatenates shot videos using ffmpeg."""

import logging
from pathlib import Path

from ffmpeg import FFmpeg

logger = logging.getLogger(__name__)


def merge_shots(shot_paths: list[str], output_path: str) -> None:
    """
    Merge multiple shot videos into a single video.

    Args:
        shot_paths: List of video file paths to concatenate
        output_path: Path for output merged video

    Raises:
        ValueError: If shot_paths is empty
        Exception: If ffmpeg fails
    """
    if not shot_paths:
        raise ValueError("No shot paths provided")

    # Filter out None or empty paths
    valid_paths = [p for p in shot_paths if p]
    if not valid_paths:
        raise ValueError("No valid shot paths provided")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if len(valid_paths) == 1:
        # Only one shot - just copy it
        (
            FFmpeg()
            .option("y")
            .input(valid_paths[0])
            .output(output_path, c="copy")
        ).execute()
        logger.info(f"Copied single shot to {output_path}")
        return

    # Create concat file list next to output (avoids /tmp lifecycle issues)
    filelist_content = "\n".join(f"file '{p}'" for p in valid_paths)
    filelist_path = Path(output_path).with_suffix(".txt")
    filelist_path.write_text(filelist_content, encoding="utf-8")

    try:
        (
            FFmpeg()
            .option("y")
            .input(str(filelist_path), f="concat", safe=0)
            .output(output_path, c="copy")
        ).execute()
        logger.info(f"Merged {len(valid_paths)} shots to {output_path}")
    finally:
        filelist_path.unlink(missing_ok=True)


def merge_shots_with_reencoding(
    shot_paths: list[str],
    output_path: str,
    codec: str = "libx264",
    preset: str = "medium",
    crf: int = 23,
) -> None:
    """
    Merge shots with re-encoding (for compatibility).

    Args:
        shot_paths: List of video file paths
        output_path: Path for output merged video
        codec: Video codec to use
        preset: Encoding preset (speed vs compression)
        crf: Constant rate factor (quality)

    Raises:
        ValueError: If shot_paths is empty
        Exception: If ffmpeg fails
    """
    if not shot_paths:
        raise ValueError("No shot paths provided")

    valid_paths = [p for p in shot_paths if p]
    if not valid_paths:
        raise ValueError("No valid shot paths provided")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    filelist_content = "\n".join(f"file '{p}'" for p in valid_paths)
    filelist_path = Path(output_path).with_suffix(".txt")
    filelist_path.write_text(filelist_content, encoding="utf-8")

    try:
        (
            FFmpeg()
            .option("y")
            .input(str(filelist_path), f="concat", safe=0)
            .output(output_path, vcodec=codec, preset=preset, crf=crf, acodec="aac")
        ).execute()
        logger.info(f"Merged {len(valid_paths)} shots with re-encoding to {output_path}")
    finally:
        filelist_path.unlink(missing_ok=True)
