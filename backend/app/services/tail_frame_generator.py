"""Tail frame generation service using Gemini image generation (two-step CoT).

Generates a target last frame for a video shot based on the director's
motion prompt, character reference images, and the starting frame.

Two-step process using gemini-3.1-flash-image-preview:
  Step 1 (TEXT): Analyze the motion prompt to derive the final pose.
  Step 2 (IMAGE): Generate the tail frame image with the analyzed pose.
"""

import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import Awaitable, Callable, List, Optional

from google import genai
from google.genai import types

from app.agents.frame_porter import center_crop_to_aspect
from app.config import settings

logger = logging.getLogger(__name__)

_client: genai.Client | None = None

# Minimum characters the CoT end-pose description must have before we accept it.
_COT_MIN_LEN = 30

# Phrases that signal the CoT tried to shortcut with a "no change" answer.
_COT_CONSERVATIVE_MARKERS = (
    "same as starting",
    "same as the starting",
    "unchanged",
    "no movement",
    "no change",
    "identical to the starting",
    "no visible change",
)


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(
            vertexai=True,
            project=settings.tf_project,
            location=settings.tf_location,
        )
    return _client


def _mime_for(path: str) -> str:
    mt, _ = mimetypes.guess_type(path)
    return mt or "image/png"


def _is_cot_too_weak(text: str) -> bool:
    stripped = (text or "").strip()
    if len(stripped) < _COT_MIN_LEN:
        return True
    lowered = stripped.lower()
    return any(marker in lowered for marker in _COT_CONSERVATIVE_MARKERS)


def _extract_text(response) -> str:
    out = ""
    for part in response.parts:
        if part.text:
            out += part.text
    return out


async def generate_tail_frame(
    character_ref_paths: List[str],
    first_frame_path: Optional[str],
    motion_prompt: str,
    output_path: str,
    object_ref_paths: Optional[List[str]] = None,
    aspect_ratio: str = "9:16",
    on_cot_complete: Optional[Callable[[str], Awaitable[None]]] = None,
) -> str:
    """Generate a target tail frame based on the director's motion prompt.

    Args:
        character_ref_paths: Paths to character reference images (identity).
        first_frame_path: Path to the shot's first frame (starting state).
        motion_prompt: Director-generated motion prompt describing the action.
        output_path: Path to write the generated tail frame image.
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

    first_frame_parts: list = []
    if first_frame_path:
        frame_data = Path(first_frame_path).read_bytes()
        first_frame_parts.append(
            types.Part.from_bytes(data=frame_data, mime_type=_mime_for(first_frame_path))
        )

    logger.info(
        "TF: calling %s  char_refs=%d  obj_refs=%d  first_frame=%s",
        model,
        len(character_ref_paths),
        len(object_ref_paths) if object_ref_paths else 0,
        first_frame_path,
    )

    # --- Step 1: CoT analysis (TEXT only) — use text model to reason about end pose ---
    # CoT only sees text output; image order doesn't affect conditioning weight,
    # so keep the legacy order here.
    cot_image_parts = char_parts + obj_parts + first_frame_parts
    cot_prompt = settings.tf_cot_prompt.format(motion_prompt=motion_prompt)
    cot_parts = cot_image_parts + [types.Part(text=cot_prompt)]

    try:
        cot_response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=settings.tf_cot_model,
                contents=[types.Content(role="user", parts=cot_parts)],
                config=types.GenerateContentConfig(temperature=0.3),
            ),
            timeout=120,
        )
    except asyncio.TimeoutError:
        raise RuntimeError("TF CoT API call timed out after 120s (network issue)")
    end_pose = _extract_text(cot_response)
    logger.info("TF CoT end pose: %s", end_pose[:500])

    # Retry once with a stronger differentiation instruction if the CoT produced
    # empty/too-short/conservative output — those correlate strongly with the
    # image step outputting a near-copy of an input image.
    if _is_cot_too_weak(end_pose):
        logger.warning("TF CoT output too weak, retrying with stronger prompt")
        retry_prompt = settings.tf_cot_retry_prompt.format(motion_prompt=motion_prompt)
        retry_parts = cot_image_parts + [types.Part(text=retry_prompt)]
        retry_response = await client.aio.models.generate_content(
            model=settings.tf_cot_model,
            contents=[types.Content(role="user", parts=retry_parts)],
            config=types.GenerateContentConfig(temperature=0.6),
        )
        retry_text = _extract_text(retry_response)
        logger.info("TF CoT retry end pose: %s", retry_text[:500])
        if not _is_cot_too_weak(retry_text):
            end_pose = retry_text
        else:
            # Hard fallback: make sure the image step still sees a strong
            # "must differ" instruction even when CoT keeps failing.
            end_pose = (
                "The character MUST be in a pose visibly different from the "
                "starting frame (different head angle, hand position, or eye "
                "direction). Base the end pose on this action: "
                f"{motion_prompt}"
            )

    if on_cot_complete:
        await on_cot_complete(end_pose)

    # --- Step 2: Image generation (IMAGE only) with CoT result ---
    # Reordered: [first_frame] (context) → [object refs] → [character refs] (identity).
    # Putting first_frame last was over-conditioning the model into "edit-this-image"
    # behavior, causing it to copy the starting pose. Character ref is now last so
    # facial identity stays strong, paired with an explicit "only features, not pose"
    # instruction in tf_prompt.
    img_image_parts = first_frame_parts + obj_parts + char_parts
    img_prompt = settings.tf_prompt.format(
        motion_prompt=motion_prompt, end_pose=end_pose,
    )
    img_parts = img_image_parts + [types.Part(text=img_prompt)]

    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=model,
                contents=[types.Content(role="user", parts=img_parts)],
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    temperature=1.0,
                ),
            ),
            timeout=120,
        )
    except asyncio.TimeoutError:
        raise RuntimeError("TF image generation timed out after 120s (network issue)")

    # Extract the generated image
    saved = False
    parts = response.parts or []

    if not parts:
        # Log block reason for debugging (safety filter, empty response, etc.)
        block_reason = getattr(response, "prompt_feedback", None)
        candidates = getattr(response, "candidates", None)
        finish_reason = None
        if candidates:
            finish_reason = getattr(candidates[0], "finish_reason", None)
        logger.error(
            "TF image generation returned no parts. "
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
            logger.info("TF done: saved %s", output_path)
            break

    if not saved:
        raise RuntimeError(
            "Gemini did not return an image. "
            f"Response parts: {[type(p).__name__ for p in parts]}"
        )

    # Gemini doesn't guarantee exact aspect ratios — center-crop to match project AR
    center_crop_to_aspect(output_path, aspect_ratio)

    return output_path
