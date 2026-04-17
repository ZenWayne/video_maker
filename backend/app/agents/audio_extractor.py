"""Audio extraction and remuxing utilities using ffmpeg."""

import logging
from pathlib import Path

from ffmpeg import FFmpeg

logger = logging.getLogger(__name__)


def extract_audio_wav(video_path: str, output_wav: str) -> str:
    """Extract audio from video as 16kHz mono WAV (required by CosyVoice VC).

    Args:
        video_path: Path to source video file
        output_wav: Path for output WAV file

    Returns:
        The output_wav path
    """
    Path(output_wav).parent.mkdir(parents=True, exist_ok=True)

    (
        FFmpeg()
        .option("y")
        .input(video_path)
        .output(output_wav, vn=None, ac=1, ar=16000)
    ).execute()

    logger.info("Extracted audio: %s -> %s", video_path, output_wav)
    return output_wav


def remux_video_with_audio(video_path: str, audio_path: str, output_path: str) -> str:
    """Replace video's audio track with new audio, copying the video stream.

    Args:
        video_path: Path to source video (video stream will be copied)
        audio_path: Path to new audio file
        output_path: Path for output video

    Returns:
        The output_path
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # ffmpeg -y -i video -i audio -c:v copy -map 0:v:0 -map 1:a:0 -shortest output
    (
        FFmpeg()
        .option("y")
        .input(video_path)
        .input(audio_path)
        .output(
            output_path,
            vcodec="copy",
            shortest=None,
            map=["0:v:0", "1:a:0"],
        )
    ).execute()

    logger.info("Remuxed: video=%s + audio=%s -> %s", video_path, audio_path, output_path)
    return output_path
