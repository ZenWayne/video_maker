"""Director Agent - generates motion prompts for each shot."""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.agents.llm import GeminiProvider
from app.config import settings

logger = logging.getLogger(__name__)


def load_system_prompt() -> str:
    """Load director system prompt from file."""
    prompt_path = Path(__file__).parent.parent.parent / "prompts" / "director.md"

    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")

    # Fallback default prompt
    return """You are a professional cinematographer specializing in AI video generation.

Your task is to create detailed motion prompts for Veo 3 video generation.

Given shot information, generate an English motion prompt that describes:
1. A fixed, locked-off camera — NO camera movement (no pan, tilt, dolly, zoom, or push-in)
2. Subject motion and actions
3. Lighting and atmosphere changes
4. Any other dynamic elements

Be specific and vivid. The prompt should be detailed enough for AI to generate smooth, cinematic motion.

Output ONLY the motion prompt text, nothing else."""


def build_user_prompt(
    shot_id: int,
    shot_type: str,
    visual_description: str,
    text: str,
    duration: int,
    num_reference_images: int = 0,
) -> str:
    """Build user prompt for director agent."""
    prompt = f"""Shot ID: {shot_id}
Shot Type: {shot_type}
Visual Description: {visual_description}
Duration: {duration} seconds
"""

    if text:
        prompt += f"Dialogue/Narration: {text}\n"

    if num_reference_images:
        prompt += (
            f"\nReference prop/object image(s) ({num_reference_images}) are attached. "
            "These are props the character holds or shows in this shot. Describe the "
            "hand/arm motion of picking up, holding, and clearly presenting these "
            "object(s) to the camera as part of the action — never leave them out.\n"
        )

    prompt += "\nGenerate a motion prompt for this shot in English:"

    return prompt


def postprocess_motion_prompt(motion_prompt: str, text: str) -> str:
    """
    Post-process motion prompt to ensure text is included.

    If text is non-empty, append '角色说：『{text}』' to the motion prompt.
    """
    if text and text.strip():
        # Check if text is already in the prompt
        if text.strip() not in motion_prompt:
            motion_prompt = motion_prompt.strip()
            if not motion_prompt.endswith("。"):
                motion_prompt += "。"
            motion_prompt += f" The character says: \"{text.strip()}\""

    return motion_prompt


async def run_director(
    shot_id: int,
    shot_type: str,
    visual_description: str,
    text: str,
    duration: int,
    llm_provider: GeminiProvider,
    reference_image_paths: Optional[List[str]] = None,
) -> str:
    """
    Generate motion prompt for a shot.

    Args:
        shot_id: Shot sequence number
        shot_type: Shot type (Close-up, Medium Shot, Wide Shot)
        visual_description: Visual description from storyboard
        text: Dialogue/text for this shot
        duration: Shot duration in seconds
        llm_provider: Gemini provider instance

    Returns:
        Motion prompt string
    """
    # Only attach reference images that exist on disk.
    refs = [p for p in (reference_image_paths or []) if p and Path(p).exists()]

    system_prompt = load_system_prompt()
    user_prompt = build_user_prompt(
        shot_id=shot_id,
        shot_type=shot_type,
        visual_description=visual_description,
        text=text,
        duration=duration,
        num_reference_images=len(refs),
    )

    logger.info("Director shot %d: %d reference image(s) attached", shot_id, len(refs))

    # Generate motion prompt
    motion_prompt = await llm_provider.generate_text(
        model=settings.gemini_director_model,
        system_prompt=system_prompt,
        user_message=user_prompt,
        temperature=0.7,
        operation="agents-director-generate-motion",
        image_paths=refs or None,
    )

    # Post-process to ensure text is included
    motion_prompt = postprocess_motion_prompt(motion_prompt, text)

    return motion_prompt
