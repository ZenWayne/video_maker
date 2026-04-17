"""VideoGenerator Agent - generates video using Veo via Vertex AI."""

import asyncio
import logging
from typing import Optional

from google import genai
from google.genai import types

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
) -> bytes:
    """Generate video using Veo via Vertex AI SDK."""
    veo_client = _get_veo_client()

    try:
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

        logger.info(
            "generate_video call: model=%s, first_frame_path=%s, reference_image_paths=%s, "
            "shot_duration=%s, prompt=%s",
            settings.veo_model, first_frame_path, reference_image_paths,
            shot_duration, prompt[:500],
        )

        if reference_image_paths:
            config.reference_images = [
                types.VideoGenerationReferenceImage(
                    image=types.Image.from_file(location=p),
                    reference_type=types.VideoGenerationReferenceType.ASSET,
                )
                for p in reference_image_paths
            ]
            operation = await veo_client.aio.models.generate_videos(
                model=settings.veo_model,
                prompt=prompt,
                config=config,
            )
        else:
            operation = await veo_client.aio.models.generate_videos(
                model=settings.veo_model,
                prompt=prompt,
                image=types.Image.from_file(location=first_frame_path) if first_frame_path else None,
                config=config,
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
            raise VideoGenerationError("No video returned from Veo")

    except (VideoGenerationTimeout, VideoGenerationError):
        raise
    except Exception as e:
        raise VideoGenerationError(f"Unexpected error during video generation: {e}")
