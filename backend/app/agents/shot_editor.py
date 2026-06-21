"""Shot Editor Agent - revises a single shot based on user instruction."""

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Union

from pydantic import BaseModel

from app.agents.llm import GeminiProvider, OpenAICompatibleProvider
from app.config import settings

logger = logging.getLogger(__name__)

LLMProvider = Union[GeminiProvider, OpenAICompatibleProvider]


class ShotEditResult(BaseModel):
    text: str
    visual_description: str


def _load_prompt() -> str:
    path = Path(__file__).parent.parent.parent / "prompts" / "shot_editor.md"
    return path.read_text(encoding="utf-8")


def _default_provider() -> LLMProvider:
    """Return DeepSeek if key is configured, otherwise fall back to Gemini."""
    if settings.deepseek_api_key:
        return OpenAICompatibleProvider(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
        )
    return GeminiProvider(project=settings.gemini_project, location=settings.gemini_location)


async def run_shot_editor(
    instruction: str,
    current_text: str,
    current_visual: str,
    shot_type: str,
    shot_duration: int,
    theme_text: str,
    scene_overview: str,
    prev_shot: Optional[Dict[str, Any]] = None,
    next_shot: Optional[Dict[str, Any]] = None,
    llm_provider: Optional[LLMProvider] = None,
    align_with_previous: bool = True,
    shot_id: int = 1,
    has_reference_images: bool = False,
) -> Dict[str, Any]:
    """Revise a shot based on user instruction and surrounding context."""
    if llm_provider is None:
        llm_provider = _default_provider()

    system_prompt = _load_prompt()

    context_parts = [
        f"Video theme: {theme_text}",
        f"Scene overview: {scene_overview}",
        "",
        "Current shot:",
        f"  Shot ID: {shot_id}  Type: {shot_type}  Duration: {shot_duration}s  Align with previous: {align_with_previous}  Has reference images: {has_reference_images}",
        f"  Text: {current_text}",
        f"  Visual: {current_visual}",
    ]

    if prev_shot:
        context_parts += [
            "",
            "Previous shot (for continuity):",
            f"  Text: {prev_shot.get('text', '')}",
            f"  Visual: {prev_shot.get('visual_description', '')}",
        ]

    if next_shot:
        context_parts += [
            "",
            "Next shot (for continuity):",
            f"  Text: {next_shot.get('text', '')}",
            f"  Visual: {next_shot.get('visual_description', '')}",
        ]

    context_parts += [
        "",
        f"Instruction: {instruction}",
        "",
        f"IMPORTANT: Your output MUST be in the same language as the original text above. Do NOT change the language.",
    ]

    user_message = "\n".join(context_parts)

    if isinstance(llm_provider, OpenAICompatibleProvider):
        result = await llm_provider.generate_json(
            system_prompt=system_prompt,
            user_message=user_message,
            response_schema=ShotEditResult,
            temperature=0.7,
            operation="agents-shot-editor-edit-shot",
        )
    else:
        result = await llm_provider.generate_json(
            model=settings.gemini_script_model,
            system_prompt=system_prompt,
            user_parts=[{"type": "text", "data": user_message}],
            response_schema=ShotEditResult,
            temperature=0.7,
            operation="agents-shot-editor-edit-shot",
        )

    return result
