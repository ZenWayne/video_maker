"""First frame generation service using Gemini image generation (two-step CoT).

Generates an opening first frame for a video shot based on the shot's visual
description, character reference images, and shot-level object references
(参考物) — mirroring the tail frame service.

Two-step process using the same models as tail frame generation:
  Step 1 (TEXT): Analyze the shot description to derive the opening composition.
  Step 2 (IMAGE): Generate the first frame image with the analyzed composition.
"""

import logging
from pathlib import Path
from typing import List, Optional

from google.genai import types

from app.agents.frame_porter import center_crop_to_aspect
from app.config import settings
from app import observability
from app.services.image_generation import (
    _call_with_timeout,
    _extract_text,
    _mime_for,
    get_client as _get_client,
)

logger = logging.getLogger(__name__)


async def generate_first_frame(
    character_ref_paths: List[str],
    context_frame_path: Optional[str],
    visual_description: str,
    shot_type: str,
    output_path: str,
    motion_prompt: Optional[str] = None,
    object_ref_paths: Optional[List[str]] = None,
    aspect_ratio: str = "9:16",
) -> str:
    """Generate an opening first frame for a shot.

    Args:
        character_ref_paths: Paths to character reference images (identity).
        context_frame_path: Optional current resolved first frame — used only
            for background/lighting/wardrobe continuity.
        visual_description: The shot's visual description (primary input).
        shot_type: Close-up / Medium Shot / Wide Shot (framing hint).
        output_path: Path to write the generated first frame image.
        motion_prompt: Optional motion prompt — the opening frame must be a
            natural starting point for it.
        object_ref_paths: Optional paths to object/prop reference images.

    Returns:
        The output_path.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    client = _get_client()
    model = settings.tf_model

    # --- Collect image parts once, keyed by role ---
    char_parts: list = []
    for ref_path in character_ref_paths:
        data = Path(ref_path).read_bytes()
        char_parts.append(types.Part.from_bytes(data=data, mime_type=_mime_for(ref_path)))

    obj_parts: list = []
    if object_ref_paths:
        for obj_path in object_ref_paths:
            p = Path(obj_path)
            if p.exists():
                data = p.read_bytes()
                obj_parts.append(types.Part.from_bytes(data=data, mime_type=_mime_for(obj_path)))

    context_parts: list = []
    if context_frame_path:
        frame_data = Path(context_frame_path).read_bytes()
        context_parts.append(
            types.Part.from_bytes(data=frame_data, mime_type=_mime_for(context_frame_path))
        )

    logger.info(
        "FF: calling %s  char_refs=%d  obj_refs=%d  context_frame=%s",
        model,
        len(character_ref_paths),
        len(object_ref_paths) if object_ref_paths else 0,
        context_frame_path,
    )

    # --- Step 1: CoT analysis (TEXT only) — reason about the opening composition ---
    cot_image_parts = char_parts + obj_parts + context_parts
    cot_prompt = settings.ff_cot_prompt.format(
        shot_type=shot_type,
        visual_description=visual_description,
        motion_prompt=motion_prompt or "(not specified)",
    )
    cot_parts = cot_image_parts + [types.Part(text=cot_prompt)]

    with observability.generation(
        name="services-first-frame-cot-analysis",
        model=settings.tf_cot_model,
        input={"visual_description": visual_description, "shot_type": shot_type},
        model_parameters={"temperature": 0.6},
    ) as cot_gen:
        cot_response = await _call_with_timeout(
            lambda: client.aio.models.generate_content(
                model=settings.tf_cot_model,
                contents=[types.Content(role="user", parts=cot_parts)],
                config=types.GenerateContentConfig(temperature=0.6),
            ),
            label="FF CoT API call",
        )
        opening_composition = _extract_text(cot_response)
        observability.update_span(cot_gen, output=opening_composition)
    logger.info("FF CoT opening composition: %s", opening_composition[:500])

    if not opening_composition.strip():
        # Hard fallback: keep the image step grounded in the shot description.
        opening_composition = (
            f"A {shot_type} opening frame matching: {visual_description}"
        )

    # --- Step 2: Image generation (IMAGE only) with CoT result ---
    # Same ordering rationale as tail frame: context first, character identity last.
    img_image_parts = context_parts + obj_parts + char_parts
    img_prompt = settings.ff_prompt.format(
        visual_description=visual_description,
        opening_composition=opening_composition,
    )
    img_parts = img_image_parts + [types.Part(text=img_prompt)]

    with observability.generation(
        name="services-first-frame-generate-image",
        model=model,
        input={"opening_composition": opening_composition},
        model_parameters={"temperature": 1.0, "response_modalities": ["IMAGE"]},
    ) as img_gen:
        response = await _call_with_timeout(
            lambda: client.aio.models.generate_content(
                model=model,
                contents=[types.Content(role="user", parts=img_parts)],
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    temperature=1.0,
                    # Pin output orientation — same rationale as tail frame:
                    # a landscape return + 9:16 center-crop reads as zoom-in.
                    image_config=types.ImageConfig(aspect_ratio=aspect_ratio),
                ),
            ),
            label="FF image generation",
        )

        saved = False
        parts = response.parts or []

        if not parts:
            block_reason = getattr(response, "prompt_feedback", None)
            candidates = getattr(response, "candidates", None)
            finish_reason = None
            if candidates:
                finish_reason = getattr(candidates[0], "finish_reason", None)
            logger.error(
                "FF image generation returned no parts. "
                "block_reason=%s  finish_reason=%s  candidates=%s",
                block_reason, finish_reason, candidates,
            )
            raise RuntimeError(
                f"Gemini returned empty response (blocked or filtered). "
                f"block_reason={block_reason}, finish_reason={finish_reason}"
            )

        for part in parts:
            if part.inline_data is not None:
                Path(output_path).write_bytes(part.inline_data.data)
                saved = True
                logger.info("FF done: saved %s", output_path)
                break

        if not saved:
            raise RuntimeError(
                "Gemini did not return an image. "
                f"Response parts: {[type(p).__name__ for p in parts]}"
            )

        observability.update_span(img_gen, output={"output_path": output_path})

    # Gemini doesn't guarantee exact aspect ratios — center-crop to match project AR
    center_crop_to_aspect(output_path, aspect_ratio)

    return output_path
