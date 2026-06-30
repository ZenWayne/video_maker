"""Frame Porter Agent - extracts and processes video frames."""

import logging
from fractions import Fraction
from pathlib import Path

from ffmpeg import FFmpeg
from PIL import Image

logger = logging.getLogger(__name__)


def center_crop_to_aspect(image_path: str, aspect_ratio: str, output_path: str | None = None) -> str:
    """Center-crop an image to the exact target aspect ratio.

    Args:
        image_path: Source image path.
        aspect_ratio: Target ratio as "W:H" (e.g. "9:16").
        output_path: Where to write the result. If None, overwrites source in-place.

    Returns:
        The output path (same as input when already correct or written in-place).
    """
    w_ratio, h_ratio = (int(x) for x in aspect_ratio.split(":"))
    target = Fraction(w_ratio, h_ratio)

    img = Image.open(image_path)
    w, h = img.size

    if Fraction(w, h) == target:
        return image_path

    if Fraction(w, h) > target:
        # Too wide → crop width
        new_w = int(h * target)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        # Too tall → crop height
        new_h = int(w / target)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))

    dest = output_path or image_path
    img.save(dest)
    logger.info(
        "Cropped %s from %dx%d to %dx%d (aspect %s) → %s",
        image_path, w, h, img.size[0], img.size[1], aspect_ratio, dest,
    )
    return dest


def extract_last_frame(video_path: str, output_path: str) -> None:
    """
    Extract the last frame from a video file.

    Uses ffprobe to find the total frame count, then a select filter to
    extract exactly the last frame.  The previous ``-sseof -0.1`` approach
    silently produced zero frames on re-encoded/trimmed videos whose
    keyframe layout didn't allow seeking that close to the end.

    Args:
        video_path: Path to input video file
        output_path: Path to output image file (PNG)

    Raises:
        RuntimeError: If ffmpeg produces no output file
        Exception: If ffmpeg fails
    """
    from app.agents.video_trimmer import get_video_info

    info = get_video_info(video_path)
    last_idx = max(0, info["total_frames"] - 1)

    try:
        (
            FFmpeg()
            .option("y")
            .input(video_path)
            .output(
                output_path,
                vf=f"select='eq(n\\,{last_idx})'",
                vsync="vfr",
                vframes=1,
                **{"q:v": 2},
            )
        ).execute()
    except Exception as e:
        logger.error(f"Failed to extract last frame: {e}")
        raise

    if not Path(output_path).exists() or Path(output_path).stat().st_size == 0:
        raise RuntimeError(
            f"extract_last_frame produced no output for {video_path} "
            f"(total_frames={info['total_frames']})"
        )
    logger.debug(f"Extracted last frame from {video_path} to {output_path}")


def extract_frame_at(video_path: str, frame_index: int, output_path: str) -> None:
    """Extract the single frame at 0-based *frame_index* as a lossless PNG.

    Used to refresh last_frame.png after a metadata-only trim: trimming to N
    frames keeps frames 0..N-1, so the new last frame is index N-1. PNG output
    is lossless, so md5 of the same (video, index) is byte-stable.
    """
    (
        FFmpeg()
        .option("y")
        .input(video_path)
        .output(
            output_path,
            vf=f"select='eq(n\\,{frame_index})'",
            vframes=1,
            vsync="0",
        )
    ).execute()
    if not Path(output_path).exists():
        raise RuntimeError(f"extract_frame_at: no frame {frame_index} in {video_path}")


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
