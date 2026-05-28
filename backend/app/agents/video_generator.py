"""VideoGenerator Agent — pluggable video providers.

Two backends implement the same ``generate_video`` contract:

* ``VertexVeoProvider`` — Veo via Google GenAI SDK on Vertex AI (local files,
  SDK long-running operation polling). Default.
* ``KieVeoProvider``  — Veo via kie.ai REST API (base64-uploads frames to get
  hosted URLs, async task + HTTP polling, downloads the result).

The active backend is chosen by ``settings.video_provider`` ("vertex" | "kie").
``generate_video()`` keeps the original module-level signature so callers
(``worker/tasks.py``) are unchanged.
"""

import asyncio
import base64
import json
import logging
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import httpx
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


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _build_prompt(motion_prompt: str, spoken_text: str) -> str:
    """Append spoken dialogue to the motion prompt (shared by all providers)."""
    if spoken_text and spoken_text.strip():
        return f"{motion_prompt}\n\nSpoken dialogue: {spoken_text.strip()}"
    return motion_prompt


def _crop_inputs(
    tmp_dir: str,
    first_frame_path: Optional[str],
    last_frame_path: Optional[str],
    reference_image_paths: Optional[list[str]],
    aspect_ratio: str,
) -> tuple[Optional[str], Optional[str], Optional[list[str]]]:
    """Center-crop all input images to the exact target aspect ratio.

    Returns the cropped (first_frame, last_frame, reference_images) paths,
    all living under ``tmp_dir``.
    """
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
    return first_frame_path, last_frame_path, reference_image_paths


# --------------------------------------------------------------------------- #
# Provider interface
# --------------------------------------------------------------------------- #

class VideoProvider(ABC):
    """A backend that turns a prompt (+ optional frames) into video bytes."""

    @abstractmethod
    async def generate_video(
        self,
        motion_prompt: str,
        first_frame_path: Optional[str],
        shot_duration: int,
        spoken_text: str,
        reference_image_paths: Optional[list[str]] = None,
        aspect_ratio: str = "16:9",
        last_frame_path: Optional[str] = None,
    ) -> bytes:
        """Generate a video and return the raw MP4 bytes."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Vertex AI Veo provider (default)
# --------------------------------------------------------------------------- #

class VertexVeoProvider(VideoProvider):
    """Veo video generation via the Google GenAI SDK on Vertex AI."""

    @staticmethod
    def _client() -> genai.Client:
        """Create a genai client (service-account creds via env)."""
        return genai.Client(
            vertexai=True,
            project=settings.veo_project,
            location=settings.veo_location,
        )

    async def generate_video(
        self,
        motion_prompt: str,
        first_frame_path: Optional[str],
        shot_duration: int,
        spoken_text: str,
        reference_image_paths: Optional[list[str]] = None,
        aspect_ratio: str = "16:9",
        last_frame_path: Optional[str] = None,
    ) -> bytes:
        veo_client = self._client()
        tmp_dir = tempfile.mkdtemp(prefix="veo_crop_")

        try:
            first_frame_path, last_frame_path, reference_image_paths = _crop_inputs(
                tmp_dir, first_frame_path, last_frame_path,
                reference_image_paths, aspect_ratio,
            )

            logger.info("Starting Veo generation with prompt: %s...", motion_prompt[:100])
            prompt = _build_prompt(motion_prompt, spoken_text)

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


# --------------------------------------------------------------------------- #
# kie.ai Veo provider (REST)
# --------------------------------------------------------------------------- #

# kie.ai Veo only accepts these durations
_KIE_DURATIONS = (4, 6, 8)


def _clamp_kie_duration(seconds: int) -> int:
    """Snap an arbitrary shot duration to the nearest kie.ai-supported value."""
    return min(_KIE_DURATIONS, key=lambda allowed: abs(allowed - seconds))


class KieVeoProvider(VideoProvider):
    """Veo video generation via the kie.ai REST API.

    Flow: base64-upload local frames -> hosted URLs, POST /api/v1/veo/generate,
    poll GET /api/v1/veo/record-info until ``successFlag`` settles, then download
    the result MP4.
    """

    UPLOAD_PATH = "/api/file-base64-upload"
    GENERATE_PATH = "/api/v1/veo/generate"
    RECORD_PATH = "/api/v1/veo/record-info"

    def _headers(self) -> dict:
        if not settings.kie_api_key:
            raise VideoGenerationError(
                "kie.ai API key not configured (set secrets/kie_api_key)"
            )
        return {
            "Authorization": f"Bearer {settings.kie_api_key}",
            "Content-Type": "application/json",
        }

    async def _upload_image(self, http: httpx.AsyncClient, path: str) -> str:
        """Base64-upload a local image to kie.ai, return its hosted download URL."""
        raw = Path(path).read_bytes()
        b64 = base64.b64encode(raw).decode("ascii")
        mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
        payload = {
            "base64Data": f"data:{mime};base64,{b64}",
            "uploadPath": "video-maker/frames",
            "fileName": Path(path).name,
        }
        resp = await http.post(
            f"{settings.kie_upload_url}{self.UPLOAD_PATH}", json=payload,
        )
        resp.raise_for_status()
        body = resp.json()
        url = (body.get("data") or {}).get("downloadUrl")
        if not body.get("success") or not url:
            raise VideoGenerationError(f"kie.ai image upload failed: {body}")
        logger.info("Uploaded %s -> %s", Path(path).name, url)
        return url

    @staticmethod
    def _resolve_mode(
        first_frame_path: Optional[str],
        last_frame_path: Optional[str],
        reference_image_paths: Optional[list[str]],
    ) -> tuple[str, list[str], str]:
        """Map our inputs to a kie.ai (generationType, image_paths, model) triple."""
        if reference_image_paths:
            # REFERENCE_2_VIDEO supports 1-3 images and requires veo3_fast
            return (
                "REFERENCE_2_VIDEO",
                reference_image_paths[:3],
                "veo3_fast",
            )
        if first_frame_path:
            # FIRST_AND_LAST_FRAMES_2_VIDEO: 1 image (unfolds) or 2 (first->last)
            images = [first_frame_path]
            if last_frame_path:
                images.append(last_frame_path)
            return "FIRST_AND_LAST_FRAMES_2_VIDEO", images, settings.kie_veo_model
        return "TEXT_2_VIDEO", [], settings.kie_veo_model

    async def _create_task(
        self, http: httpx.AsyncClient, prompt: str, generation_type: str,
        image_urls: list[str], model: str, duration: int, aspect_ratio: str,
    ) -> str:
        body: dict = {
            "prompt": prompt,
            "model": model,
            "generationType": generation_type,
            "aspect_ratio": aspect_ratio if aspect_ratio in ("16:9", "9:16") else "Auto",
            "resolution": settings.kie_resolution,
            "duration": duration,
        }
        if image_urls:
            body["imageUrls"] = image_urls

        logger.info(
            "kie.ai generate: model=%s, type=%s, images=%d, duration=%s, prompt=%s",
            model, generation_type, len(image_urls), duration, prompt[:500],
        )
        resp = await http.post(f"{settings.kie_base_url}{self.GENERATE_PATH}", json=body)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 200:
            raise VideoGenerationError(f"kie.ai generate rejected: {data}")
        task_id = (data.get("data") or {}).get("taskId")
        if not task_id:
            raise VideoGenerationError(f"kie.ai generate returned no taskId: {data}")
        logger.info("kie.ai task created: %s", task_id)
        return task_id

    async def _poll_result(self, http: httpx.AsyncClient, task_id: str) -> str:
        """Poll record-info until done; return the result video URL."""
        elapsed = 0
        poll_interval = settings.kie_poll_interval_seconds
        max_wait = settings.kie_max_wait_seconds

        while True:
            resp = await http.get(
                f"{settings.kie_base_url}{self.RECORD_PATH}",
                params={"taskId": task_id},
            )
            resp.raise_for_status()
            data = resp.json().get("data") or {}
            flag = data.get("successFlag")

            # successFlag: 0=generating, 1=success, 2=failed, 3=upstream gen failed
            if flag == 1:
                response = data.get("response") or {}
                urls = response.get("resultUrls") or response.get("fullResultUrls")
                if isinstance(urls, str):  # kie.ai sometimes returns a JSON string
                    urls = json.loads(urls)
                if not urls:
                    raise VideoGenerationError(
                        f"kie.ai task {task_id} succeeded but returned no result URL: {data}"
                    )
                return urls[0]
            if flag in (2, 3):
                raise VideoGenerationError(
                    f"kie.ai task {task_id} failed "
                    f"(flag={flag}, code={data.get('errorCode')}): "
                    f"{data.get('errorMessage')}"
                )

            if elapsed >= max_wait:
                raise VideoGenerationTimeout(
                    f"kie.ai task {task_id} timed out after {max_wait}s"
                )
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            logger.debug("Polling kie.ai task %s... elapsed=%ds", task_id, elapsed)

    async def generate_video(
        self,
        motion_prompt: str,
        first_frame_path: Optional[str],
        shot_duration: int,
        spoken_text: str,
        reference_image_paths: Optional[list[str]] = None,
        aspect_ratio: str = "16:9",
        last_frame_path: Optional[str] = None,
    ) -> bytes:
        tmp_dir = tempfile.mkdtemp(prefix="kie_crop_")
        api_timeout = 120  # seconds per HTTP request

        try:
            first_frame_path, last_frame_path, reference_image_paths = _crop_inputs(
                tmp_dir, first_frame_path, last_frame_path,
                reference_image_paths, aspect_ratio,
            )

            prompt = _build_prompt(motion_prompt, spoken_text)
            generation_type, image_paths, model = self._resolve_mode(
                first_frame_path, last_frame_path, reference_image_paths,
            )
            duration = (
                8 if generation_type == "REFERENCE_2_VIDEO"
                else _clamp_kie_duration(shot_duration)
            )

            timeout = httpx.Timeout(api_timeout)
            async with httpx.AsyncClient(headers=self._headers(), timeout=timeout) as http:
                image_urls = [await self._upload_image(http, p) for p in image_paths]
                task_id = await self._create_task(
                    http, prompt, generation_type, image_urls, model,
                    duration, aspect_ratio,
                )
                result_url = await self._poll_result(http, task_id)

                logger.info("Downloading kie.ai result: %s", result_url)
                # Result URL is public — no auth header needed
                async with httpx.AsyncClient(timeout=timeout) as dl:
                    video_resp = await dl.get(result_url)
                    video_resp.raise_for_status()
                    video_bytes = video_resp.content

            logger.info("Video generated successfully (kie.ai): %d bytes", len(video_bytes))
            return video_bytes

        except (VideoGenerationTimeout, VideoGenerationError):
            raise
        except httpx.HTTPError as e:
            raise VideoGenerationError(f"kie.ai HTTP error: {e}")
        except Exception as e:
            raise VideoGenerationError(f"Unexpected error during video generation: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Factory + backward-compatible entry point
# --------------------------------------------------------------------------- #

_PROVIDERS: dict[str, type[VideoProvider]] = {
    "vertex": VertexVeoProvider,
    "kie": KieVeoProvider,
}


def get_video_provider() -> VideoProvider:
    """Return the video provider selected by ``settings.video_provider``."""
    provider_cls = _PROVIDERS.get(settings.video_provider)
    if provider_cls is None:
        raise VideoGenerationError(
            f"Unknown video_provider '{settings.video_provider}' "
            f"(expected one of {sorted(_PROVIDERS)})"
        )
    return provider_cls()


async def generate_video(
    client=None,
    motion_prompt: str = "",
    first_frame_path: Optional[str] = None,
    shot_duration: int = 8,
    spoken_text: str = "",
    operation_id: Optional[str] = None,
    reference_image_paths: Optional[list[str]] = None,
    aspect_ratio: str = "16:9",
    last_frame_path: Optional[str] = None,
) -> bytes:
    """Generate video via the configured provider.

    ``client`` and ``operation_id`` are accepted for backward compatibility and
    ignored — each provider manages its own client/transport.
    """
    provider = get_video_provider()
    return await provider.generate_video(
        motion_prompt=motion_prompt,
        first_frame_path=first_frame_path,
        shot_duration=shot_duration,
        spoken_text=spoken_text,
        reference_image_paths=reference_image_paths,
        aspect_ratio=aspect_ratio,
        last_frame_path=last_frame_path,
    )
