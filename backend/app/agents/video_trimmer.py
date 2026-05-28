"""Video Trimmer — ffprobe metadata and FFmpeg frame-precise trimming."""

import json
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ffmpeg import FFmpeg

logger = logging.getLogger(__name__)


def detect_speech_end(
    video_path: str,
    silence_threshold_db: float = -30,
    min_silence_duration: float = 0.3,
) -> float | None:
    """Detect the timestamp where the last speech segment ends.

    Uses ffmpeg silencedetect to find silence periods. Only considers
    trailing silence (silence that extends to the end of the video).
    Ignores silence segments in the middle or spanning the entire file.

    Returns:
        Timestamp in seconds where trailing silence begins (= speech end),
        or None if no valid trailing silence found.
    """
    from app.agents.video_trimmer import get_video_info
    info = get_video_info(video_path)
    video_duration = info["duration"]

    result = subprocess.run(
        [
            "ffmpeg", "-i", video_path,
            "-af", f"silencedetect=noise={silence_threshold_db}dB:d={min_silence_duration}",
            "-f", "null", "-",
        ],
        capture_output=True, text=True,
    )
    output = result.stderr

    # Parse silence_start and silence_end pairs
    starts = [float(m.group(1)) for m in re.finditer(r"silence_start:\s*([\d.]+)", output)]
    ends = [float(m.group(1)) for m in re.finditer(r"silence_end:\s*([\d.]+)", output)]

    if not starts:
        return None

    # The last silence_start has no matching silence_end → it's trailing silence
    # (silence that runs to the end of the file)
    is_trailing = len(starts) > len(ends)

    if not is_trailing:
        # All silence segments have both start and end → no trailing silence
        logger.info("No trailing silence in %s (all segments have end markers)", video_path)
        return None

    last_start = starts[-1]

    # Guard: if trailing silence starts at 0 (entire file is silent), skip
    if last_start < 0.5:
        logger.info("Entire file appears silent (silence_start=%.2fs), skipping trim", last_start)
        return None

    logger.info("Speech end detected at %.2fs in %s (duration=%.2fs)", last_start, video_path, video_duration)
    return last_start


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


def trim_video(input_path: str, output_path: str, num_frames: int) -> None:
    """Trim video to exactly *num_frames* video frames.

    Uses ``-vframes`` for frame-precise cutting (time-based ``-t`` suffers
    from float rounding and can be ±1 frame off).  Re-encodes with libx264
    crf=18 because stream copy can only cut at keyframes.
    """
    try:
        (
            FFmpeg()
            .option("y")
            .input(input_path)
            .output(
                output_path,
                vframes=num_frames,
                vcodec="libx264",
                preset="fast",
                crf=18,
                acodec="aac",
            )
        ).execute()
        logger.info("Trimmed %s → %s (%d frames)", input_path, output_path, num_frames)
    except Exception as e:
        logger.error("Failed to trim video: %s", e)
        raise


def find_best_tail_frame(
    video_path: str,
    target_frame_path: str,
    search_frames: int = 30,
    trim_after_speech: bool = True,
    speech_window: float = 0.1,
) -> int | None:
    """Find the frame in the video's tail that best matches *target_frame_path* via SSIM.

    When *trim_after_speech* is True, searches a small window (default 0.1s)
    starting right at the silence onset for the best SSIM match. Falls back
    to the last *search_frames* if no speech boundary is detected.

    Returns:
        The total number of frames to keep (i.e. trim point, 1-based for
        ``-vframes``), or ``None`` if the best match is already the last frame.
    """
    from skimage.io import imread
    from skimage.color import rgb2gray
    from skimage.metrics import structural_similarity
    from skimage.transform import resize

    info = get_video_info(video_path)
    total = info["total_frames"]
    fps = info["fps"]

    # Determine search range: prefer speech-end boundary
    start_frame = total - min(search_frames, total)
    end_frame = total  # exclusive
    if trim_after_speech:
        speech_end = detect_speech_end(video_path)
        if speech_end is not None:
            # Search within a tight window right at silence onset
            se_frame = int(speech_end * fps)
            window_frames = max(int(speech_window * fps), 1)
            if se_frame < total - 1:
                start_frame = se_frame
                end_frame = min(se_frame + window_frames, total)
                logger.info(
                    "SSIM search range: frame %d–%d (speech ends at %.2fs, window %.1fs)",
                    start_frame, end_frame - 1, speech_end, speech_window,
                )

    # Read and prepare target image
    target = rgb2gray(imread(target_frame_path))

    with tempfile.TemporaryDirectory() as tmp_dir:
        # Extract frames in the search range
        select_expr = (
            f"between(n\\,{start_frame}\\,{end_frame - 1})"
        )
        (
            FFmpeg()
            .option("y")
            .input(video_path)
            .output(
                str(Path(tmp_dir) / "%04d.png"),
                vf=f"select='{select_expr}'",
                vsync="vfr",
                **{"q:v": 2},
            )
        ).execute()

        frame_files = sorted(Path(tmp_dir).glob("*.png"))
        if not frame_files:
            logger.warning("No frames extracted from %s", video_path)
            return None

        best_idx = 0
        best_ssim = -1.0

        for i, fp in enumerate(frame_files):
            frame = rgb2gray(imread(str(fp)))
            if frame.shape != target.shape:
                frame = resize(frame, target.shape, anti_aliasing=True)
            score = structural_similarity(target, frame, data_range=1.0)
            if score > best_ssim:
                best_ssim = score
                best_idx = i

        best_global = start_frame + best_idx
        logger.info(
            "Tail-frame SSIM search: best=frame %d (ssim=%.4f), search=[%d–%d], total=%d",
            best_global, best_ssim, start_frame, end_frame - 1, total,
        )

        if best_global >= total - 1:
            return None

        return best_global + 1


def auto_trim_to_tail_frame(
    video_path: str,
    target_frame_path: str,
    search_frames: int = 30,
) -> dict | None:
    """Find the best tail-frame match and trim the video in-place.

    Returns:
        A dict with ``trimmed_to_frame`` and video info, or ``None`` if no
        trimming was needed.
    """
    best_frames = find_best_tail_frame(video_path, target_frame_path, search_frames)
    if best_frames is None:
        return None

    vp = Path(video_path)
    backup = vp.with_name("output_original.mp4")
    if not backup.exists():
        shutil.copy2(str(vp), str(backup))
    tmp_out = vp.with_suffix(".trimmed.mp4")
    try:
        trim_video(video_path, str(tmp_out), best_frames)
        shutil.move(str(tmp_out), str(vp))
    finally:
        if tmp_out.exists():
            tmp_out.unlink()

    return {"trimmed_to_frame": best_frames, **get_video_info(video_path)}
