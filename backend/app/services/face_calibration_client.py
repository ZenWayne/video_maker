"""Face calibration service using Gemini image generation.

Corrects face drift in a shot's last frame using a two-image approach
with Gemini's native image generation (gemini-3.1-flash-image-preview):

  [Image 1] — Shot last frame (BASE — preserve pose, hands, background)
  [Image 2] — Character reference image (identity donor — only face features)

The target frame is passed FIRST so Gemini treats it as the base image.
The prompt instructs Gemini to only swap facial identity from [Image 2]
while keeping everything else from [Image 1] pixel-identical.
"""

import logging
import mimetypes
from pathlib import Path
from typing import List

from google import genai
from google.genai import types

from app.config import settings
from app import observability

logger = logging.getLogger(__name__)

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(
            vertexai=True,
            project=settings.cc_project,
            location=settings.cc_location,
        )
    return _client


def _mime_for(path: str) -> str:
    mt, _ = mimetypes.guess_type(path)
    return mt or "image/png"


async def calibrate_face(
    reference_image_paths: List[str],
    source_frame_path: str,
    output_frame_path: str,
) -> str:
    """Calibrate face in *source_frame* to match character reference images.

    [Image 1] = reference_image_paths (identity donor)
    [Image 2] = source_frame_path    (target frame — preserve expression/pose)

    Calls Gemini generate_content with both images + cc_prompt, then saves
    the returned image to *output_frame_path*.

    Args:
        reference_image_paths: Absolute paths to character reference images.
        source_frame_path: Path to the shot's last_frame.png.
        output_frame_path: Path to write the calibrated image.

    Returns:
        The *output_frame_path*.
    """
    Path(output_frame_path).parent.mkdir(parents=True, exist_ok=True)

    client = _get_client()
    prompt = settings.cc_prompt
    model = settings.cc_model

    # Build contents: [Image 1] target frame (BASE) + [Image 2..N] identity refs + prompt
    contents: list = []

    # [Image 1] — target frame (BASE image, preserve pose/hands/background)
    frame_data = Path(source_frame_path).read_bytes()
    contents.append(types.Part.from_bytes(data=frame_data, mime_type=_mime_for(source_frame_path)))

    # [Image 2..N] — identity reference(s) (only extract face features)
    for ref_path in reference_image_paths:
        data = Path(ref_path).read_bytes()
        contents.append(types.Part.from_bytes(data=data, mime_type=_mime_for(ref_path)))

    # Prompt text
    contents.append(types.Part(text=prompt))

    logger.info(
        "CC: calling %s  refs=%d  frame=%s",
        model,
        len(reference_image_paths),
        source_frame_path,
    )

    with observability.generation(
        name="services-face-calibration-generate-image",
        model=model,
        input={"source_frame": source_frame_path, "num_refs": len(reference_image_paths)},
        model_parameters={"response_modalities": ["IMAGE"]},
    ) as gen:
        response = await client.aio.models.generate_content(
            model=model,
            contents=[types.Content(role="user", parts=contents)],
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
            ),
        )

        # Extract the generated image from response
        saved = False
        parts = response.parts or []
        for part in parts:
            if part.inline_data is not None:
                Path(output_frame_path).write_bytes(part.inline_data.data)
                saved = True
                logger.info("CC done: saved %s", output_frame_path)
                break
            if part.text is not None:
                logger.info("CC text response: %s", part.text[:200])

        if not saved:
            observability.update_span(
                gen, level="ERROR", status_message="Gemini did not return an image"
            )
            raise RuntimeError(
                "Gemini did not return an image. "
                f"Response parts: {[type(p).__name__ for p in parts]}"
            )

        observability.update_span(gen, output={"output_path": output_frame_path})

    return output_frame_path
