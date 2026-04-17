"""Frame Porter Agent - extracts last frame from video using ffmpeg."""

import logging
from pathlib import Path

from ffmpeg import FFmpeg

logger = logging.getLogger(__name__)


def extract_last_frame(video_path: str, output_path: str) -> None:
    """
    Extract the last frame from a video file.

    Args:
        video_path: Path to input video file
        output_path: Path to output image file (PNG)

    Raises:
        Exception: If ffmpeg fails
    """
    try:
        (
            FFmpeg()
            .option("y")
            .input(video_path, sseof=-0.1)
            .output(output_path, {"q:v": 2}, vframes=1)
        ).execute()
        logger.debug(f"Extracted last frame from {video_path} to {output_path}")
    except Exception as e:
        logger.error(f"Failed to extract last frame: {e}")
        raise


def extract_frame_at_time(video_path: str, output_path: str, time_seconds: float) -> None:
    """
    Extract a frame at a specific time from a video file.

    Args:
        video_path: Path to input video file
        output_path: Path to output image file
        time_seconds: Time in seconds to extract frame at

    Raises:
        Exception: If ffmpeg fails
    """
    try:
        (
            FFmpeg()
            .option("y")
            .input(video_path, ss=time_seconds)
            .output(output_path, {"q:v": 2}, vframes=1)
        ).execute()
        logger.debug(f"Extracted frame at {time_seconds}s from {video_path}")
    except Exception as e:
        logger.error(f"Failed to extract frame: {e}")
        raise
