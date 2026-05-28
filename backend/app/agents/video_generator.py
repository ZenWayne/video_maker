"""VideoGenerator Agent - generates video using Veo via Vertex AI."""

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types

from app.agents.frame_porter import center_crop_to_aspect
from app.config import settings

logger = logging.getLogger(__name__)


class VideoGenerationError(Exception):
    """Raised when video generation fails."""
    pass


class VideoGenerationTimeout(Exception):
    """Raised when video generation times out."""
    pass


def _get_veo_client() -> genai.Client:
    """Create a genai client for Veo video generation via Vertex AI.

    Uses service account credentials (GOOGLE_APPLICATION_CREDENTIALS env var)
    with explicit project and location.
    """
    return genai.Client(
        vertexai=True,
        project=settings.veo_project,
        location=settings.veo_location,
    )


async def generate_video(
    client,
    motion_prompt: str,
    first_frame_path: Optional[str],
    shot_duration: int,
    spoken_text: str,
    operation_id: Optional[str] = None,
    reference_image_paths: Optional[list[str]] = None,
    aspect_ratio: str = "16:9",
    last_frame_path: Optional[str] = None,
) -> bytes:
    """Generate video using Veo via Vertex AI SDK."""
    veo_client = _get_veo_client()
    tmp_dir = tempfile.mkdtemp(prefix="veo_crop_")

    try:
        # Crop input images to exact aspect ratio before sending to Veo
        if first_frame_path:
            first_frame_path = center_crop_to_aspect(
                first_frame_path, aspect_ratio,
                output_path=str(Path(tmp_dir) / Path(first_frame_path).name),
            )
        if last_frame_path:
            last_frame_path = center_crop_to_aspect(
                last_frame_path, aspect_ratio,
                output_path=str(Path(tmp_dir) / Path(last_frame_path).name),
            )
        if reference_image_paths:
            reference_image_paths = [
                center_crop_to_aspect(
                    p, aspect_ratio,
                    output_path=str(Path(tmp_dir) / f"{i}_{Path(p).name}"),
                )
                for i, p in enumerate(reference_image_paths)
            ]

        logger.info("Starting Veo generation with prompt: %s...", motion_prompt[:100])

        prompt = motion_prompt
        if spoken_text and spoken_text.strip():
            prompt = f"{motion_prompt}\n\nSpoken dialogue: {spoken_text.strip()}"

        # reference_images mode requires exactly 8s duration
        effective_duration = 8 if reference_image_paths else shot_duration

        config = types.GenerateVideosConfig(
            aspect_ratio=aspect_ratio,
            duration_seconds=effective_duration,
            number_of_videos=1,
        )

        # Set target last frame for image-to-video mode (frame interpolation)
        if last_frame_path and not reference_image_paths:
            config.last_frame = types.Image.from_file(location=last_frame_path)

        logger.info(
            "generate_video call: model=%s, first_frame_path=%s, reference_image_paths=%s, "
            "last_frame_path=%s, shot_duration=%s, prompt=%s",
            settings.veo_model, first_frame_path, reference_image_paths,
            last_frame_path, shot_duration, prompt[:500],
        )

        # Wrap API call with timeout to prevent SSL hangs
        api_timeout = 120  # seconds for the initial API call
        try:
            if reference_image_paths:
                config.reference_images = [
                    types.VideoGenerationReferenceImage(
                        image=types.Image.from_file(location=p),
                        reference_type=types.VideoGenerationReferenceType.ASSET,
                    )
                    for p in reference_image_paths
                ]
                operation = await asyncio.wait_for(
                    veo_client.aio.models.generate_videos(
                        model=settings.veo_model,
                        prompt=prompt,
                        config=config,
                    ),
                    timeout=api_timeout,
                )
            else:
                operation = await asyncio.wait_for(
                    veo_client.aio.models.generate_videos(
                        model=settings.veo_model,
                        prompt=prompt,
                        image=types.Image.from_file(location=first_frame_path) if first_frame_path else None,
                        config=config,
                    ),
                    timeout=api_timeout,
                )
        except asyncio.TimeoutError:
            raise VideoGenerationTimeout(
                f"Veo API call timed out after {api_timeout}s (network issue)"
            )

        # Poll for completion
        elapsed = 0
        poll_interval = settings.veo_poll_interval_seconds
        max_wait = settings.veo_max_wait_seconds

        while not operation.done:
            if elapsed >= max_wait:
                raise VideoGenerationTimeout(
                    f"Veo operation timed out after {max_wait} seconds"
                )
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            logger.debug("Polling Veo operation... elapsed=%ds", elapsed)
            operation = await veo_client.aio.operations.get(operation)

        if operation.error:
            raise VideoGenerationError(f"Veo generation failed: {operation.error}")

        if (
            operation.response
            and operation.response.generated_videos
            and len(operation.response.generated_videos) > 0
        ):
            video = operation.response.generated_videos[0].video

            if video.video_bytes:
                video_bytes = video.video_bytes
            elif video.uri:
                logger.info("Downloading video from URI: %s", video.uri)
                video_bytes = await veo_client.aio.files.download(file=video.uri)
            else:
                raise VideoGenerationError("No video data or URI returned from Veo")

            logger.info("Video generated successfully: %d bytes", len(video_bytes))
            return video_bytes
        else:
            logger.error(
                "No video returned from Veo. response=%s",
                operation.response,
            )
            raise VideoGenerationError("No video returned from Veo")

    except (VideoGenerationTimeout, VideoGenerationError):
        raise
    except Exception as e:
        raise VideoGenerationError(f"Unexpected error during video generation: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
