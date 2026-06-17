"""Merger Agent - concatenates shot videos using ffmpeg."""

import logging
import subprocess
from pathlib import Path

from ffmpeg import FFmpeg

logger = logging.getLogger(__name__)


def _get_durations(shot_paths: list[str]) -> list[float]:
    """Get frame-based duration (total_frames / fps) for each shot video.

    Uses frame count instead of container duration to avoid mismatch
    with xfade which operates on video frames, not container timestamps.
    """
    from app.agents.video_trimmer import get_video_info
    durations = []
    for p in shot_paths:
        info = get_video_info(p)
        durations.append(info["total_frames"] / info["fps"])
    return durations


def merge_shots_with_crossfade(
    shot_paths: list[str],
    output_path: str,
    crossfade_duration: float = 0.3,
    codec: str = "libx264",
    preset: str = "medium",
    crf: int = 18,
) -> None:
    """Merge shots with crossfade transitions between consecutive clips.

    Uses ffmpeg xfade (video) + acrossfade (audio) filters chained for N inputs.
    Falls back to re-encoding concat when crossfade_duration <= 0.
    """
    if not shot_paths:
        raise ValueError("No shot paths provided")

    valid_paths = [p for p in shot_paths if p]
    if not valid_paths:
        raise ValueError("No valid shot paths provided")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if len(valid_paths) == 1:
        from ffmpeg import FFmpeg as _FF
        (_FF().option("y").input(valid_paths[0]).output(output_path, c="copy")).execute()
        logger.info("Copied single shot to %s", output_path)
        return

    if crossfade_duration <= 0:
        merge_shots_with_reencoding(valid_paths, output_path, codec, preset, crf)
        return

    durations = _get_durations(valid_paths)

    # Clamp crossfade to half the shortest shot
    min_dur = min(durations)
    effective_cf = min(crossfade_duration, min_dur / 2)
    if effective_cf != crossfade_duration:
        logger.warning(
            "Crossfade clamped from %.2fs to %.2fs (shortest shot %.2fs)",
            crossfade_duration, effective_cf, min_dur,
        )

    n = len(valid_paths)
    d = effective_cf

    # Build filter_complex
    vfilters = []
    afilters = []

    # Video: chain xfade
    # First pair
    cumulative = durations[0] - d
    vfilters.append(
        f"[0:v][1:v]xfade=transition=fade:duration={d}:offset={cumulative:.4f}[v01]"
    )
    prev_v = "v01"

    for i in range(2, n):
        cumulative += durations[i - 1] - d
        label = f"v{i}"
        vfilters.append(
            f"[{prev_v}][{i}:v]xfade=transition=fade:duration={d}:offset={cumulative:.4f}[{label}]"
        )
        prev_v = label

    # Audio: trim each audio to match video frame duration, then chain acrossfade
    atrim_filters = []
    for i in range(n):
        atrim_filters.append(f"[{i}:a]atrim=0:{durations[i]:.4f},asetpts=PTS-STARTPTS[at{i}]")

    afilters.append(f"[at0][at1]acrossfade=d={d}:c1=tri:c2=tri[a01]")
    prev_a = "a01"

    for i in range(2, n):
        label = f"a{i}"
        afilters.append(f"[{prev_a}][at{i}]acrossfade=d={d}:c1=tri:c2=tri[{label}]")
        prev_a = label

    filter_complex = ";".join(atrim_filters + vfilters + afilters)

    # Build ffmpeg command
    cmd = ["ffmpeg", "-y"]
    for p in valid_paths:
        cmd += ["-i", str(Path(p).resolve())]
    cmd += [
        "-filter_complex", filter_complex,
        "-map", f"[{prev_v}]",
        "-map", f"[{prev_a}]",
        "-c:v", codec,
        "-preset", preset,
        "-crf", str(crf),
        "-c:a", "aac",
        output_path,
    ]

    logger.info("Crossfade merge: %d shots, duration=%.2fs, cmd=%s", n, d, " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("Crossfade merge failed: %s", result.stderr[-500:])
        raise RuntimeError(f"ffmpeg crossfade failed: {result.stderr[-300:]}")

    logger.info("Merged %d shots with %.2fs crossfade to %s", n, d, output_path)


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

    # Create concat file list next to output (avoids /tmp lifecycle issues).
    # Use absolute paths because ffmpeg concat resolves relative paths
    # relative to the concat file's directory, not the working directory.
    filelist_content = "\n".join(
        f"file '{Path(p).resolve()}'" for p in valid_paths
    )
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

    filelist_content = "\n".join(
        f"file '{Path(p).resolve()}'" for p in valid_paths
    )
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
