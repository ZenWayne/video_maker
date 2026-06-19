"""Screenwriter Agent - generates storyboard from theme and reference images."""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.agents.llm import GeminiProvider
from app.config import settings

logger = logging.getLogger(__name__)


class ShotItem(BaseModel):
    """Single shot in storyboard."""

    shot_id: int = Field(..., ge=1)
    text: str  # Dialogue/text
    shot_type: str = Field(..., pattern="^(Close-up|Medium Shot|Wide Shot)$")
    visual_description: str
    shot_duration: int = Field(..., ge=4, le=8)
    align_with_previous: bool = True
    reference_image_hint: Optional[str] = None


class Storyboard(BaseModel):
    """Complete storyboard output."""

    scene_overview: str
    shots: List[ShotItem]


# Word count rules by duration (English word count)
# Normal speaking pace ≈ 2.6 words/sec
WORD_COUNT_RULES = {
    4: (8, 10),
    6: (13, 16),
    8: (18, 21),
}


def validate_word_count(text: str, duration: int) -> bool:
    """
    Check if text word count is within recommended range for duration.

    Args:
        text: The dialogue text
        duration: Shot duration in seconds

    Returns:
        True if within range, False if exceeds
    """
    if duration not in WORD_COUNT_RULES:
        return True

    min_chars, max_chars = WORD_COUNT_RULES[duration]
    char_count = len(text.strip().split())

    return min_chars <= char_count <= max_chars


def load_system_prompt() -> str:
    """Load screenwriter system prompt from file."""
    prompt_path = Path(__file__).parent.parent.parent / "prompts" / "screenwriter.md"

    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")

    # Fallback default prompt
    return """You are a professional video storyboard writer.

Your task is to create a detailed storyboard based on the user's theme and reference images.

Output a JSON object with:
- scene_overview: A brief description of the overall scene
- shots: An array of shot objects, each containing:
  - shot_id: Sequential number starting from 1
  - text: Dialogue or narration for this shot
  - shot_type: One of "Close-up", "Medium Shot", "Wide Shot"
  - visual_description: Detailed description of actions and expressions
  - shot_duration: Duration in seconds (4, 6, or 8)
  - align_with_previous: true if this shot continues from the previous shot (same action/angle), false for cuts/transitions

For align_with_previous:
- Set to true if this shot is a continuation of the previous shot (e.g., continuous dialogue, same action flow)
- Set to false for cuts to new angles, scene changes, or montage transitions
- Shot 1 should always have align_with_previous = false (it will be ignored anyway)

Keep text concise and appropriate for the duration."""


async def run_screenwriter(
    theme_text: str,
    reference_images: List[Dict[str, Any]],
    llm_provider: GeminiProvider,
    aspect_ratio: str = "16:9",
) -> Dict[str, Any]:
    """
    Generate storyboard from theme and reference images.

    Args:
        theme_text: User's theme/description
        reference_images: List of reference image dicts with 'kind', 'path', 'filename'
        llm_provider: Gemini provider instance
        aspect_ratio: Video aspect ratio (e.g., "16:9", "9:16")

    Returns:
        Dictionary with storyboard data and word_count_warnings
    """
    system_prompt = load_system_prompt()

    # Build user message parts
    user_parts = []

    # Add reference images
    for i, img in enumerate(reference_images):
        kind_label = "角色" if img["kind"] == "character" else "场景"
        user_parts.append({"type": "text", "data": f"{kind_label}参考图 {i + 1}:"})
        user_parts.append(
            {
                "type": "image_file",
                "data": img["path"],
                "mime_type": "image/png",
            }
        )

    # Add theme text and aspect ratio
    user_parts.append({"type": "text", "data": f"主题：{theme_text}"})
    user_parts.append(
        {
            "type": "text",
            "data": f"画面比例：{aspect_ratio}{'（横屏）' if aspect_ratio == '16:9' else '（竖屏）'}",
        }
    )

    # Generate storyboard
    result = await llm_provider.generate_json(
        model=settings.gemini_script_model,
        system_prompt=system_prompt,
        user_parts=user_parts,
        response_schema=Storyboard,
        temperature=0.7,
        operation="agents-screenwriter-generate-storyboard",
    )

    # Validate word counts and mark warnings
    word_count_warnings = []
    for shot in result.get("shots", []):
        text = shot.get("text", "")
        duration = shot.get("shot_duration", 4)

        if not validate_word_count(text, duration):
            word_count_warnings.append(
                {
                    "shot_id": shot["shot_id"],
                    "text_length": len(text),
                    "duration": duration,
                    "recommended": WORD_COUNT_RULES.get(duration),
                }
            )
            shot["word_count_warning"] = True
        else:
            shot["word_count_warning"] = False

    return {
        "storyboard": result,
        "word_count_warnings": word_count_warnings,
    }
