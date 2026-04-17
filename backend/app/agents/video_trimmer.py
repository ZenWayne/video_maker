"""Video Trimmer — ffprobe metadata and FFmpeg frame-precise trimming."""

import json
import logging
import subprocess
from pathlib import Path

from ffmpeg import FFmpeg

logger = logging.getLogger(__name__)


def get_video_info(video_path: str) -> dict:
    """Get video metadata using ffprobe.

    Returns:
        dict with keys: fps (float), total_frames (int), duration (float)
    """
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_format", video_path,
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr}")

    data = json.loads(result.stdout)
    video_stream = next(
        s for s in data["streams"] if s["codec_type"] == "video"
    )

    fps_parts = video_stream["r_frame_rate"].split("/")
    fps = float(fps_parts[0]) / float(fps_parts[1])
    duration = float(data["format"]["duration"])
    total_frames = int(video_stream.get("nb_frames", round(duration * fps)))

    return {
        "fps": fps,
        "total_frames": total_frames,
        "duration": round(duration, 4),
    }


def trim_video(input_path: str, output_path: str, end_time: float) -> None:
    """Trim video to end at end_time seconds with frame-level precision.

    Uses re-encoding (libx264 crf=18) because stream copy can only cut
    at keyframes, which is too coarse for frame-level control.
    """
    try:
        (
            FFmpeg()
            .option("y")
            .input(input_path)
            .output(
                output_path,
                t=end_time,
                vcodec="libx264",
                preset="fast",
                crf=18,
                acodec="aac",
            )
        ).execute()
        logger.info("Trimmed %s → %s (end_time=%.3fs)", input_path, output_path, end_time)
    except Exception as e:
        logger.error("Failed to trim video: %s", e)
        raise
