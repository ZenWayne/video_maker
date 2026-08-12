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

# 中文口播按「字」计，正常语速 ≈ 4.0–5.0 字/秒。
# 中文逐字稿套用英文词数上限会导致内容量只有一半，是逐字稿显得空洞的原因之一。
CJK_CHAR_COUNT_RULES = {
    4: (16, 20),
    6: (24, 30),
    8: (32, 40),
}


def _cjk_char_count(text: str) -> int:
    """统计 CJK 汉字数（不含标点）。"""
    return sum(1 for ch in text if "一" <= ch <= "鿿")


def count_speech_units(text: str) -> tuple[int, str]:
    """返回 (计数, 单位)。中文为主 → 按汉字计；否则按空格分词计。

    `str.split()` 对中文恒等于 1，直接套英文词数规则等于没有校验。
    """
    stripped = (text or "").strip()
    cjk = _cjk_char_count(stripped)
    words = len(stripped.split())
    if cjk > 0 and cjk >= words:
        return cjk, "cjk_chars"
    return words, "words"


def speech_budget(text: str, duration: int) -> tuple[int, str, Optional[tuple[int, int]]]:
    """返回 (实际计数, 单位, 该时长对应的推荐区间)；未知时长 → 区间为 None。"""
    count, unit = count_speech_units(text)
    table = CJK_CHAR_COUNT_RULES if unit == "cjk_chars" else WORD_COUNT_RULES
    return count, unit, table.get(duration)


def validate_word_count(text: str, duration: int) -> bool:
    """
    Check if text length is within the recommended range for the duration.

    中英文分别按字 / 按词计（见 count_speech_units）。

    Args:
        text: The dialogue text
        duration: Shot duration in seconds

    Returns:
        True if within range (or duration unknown), False otherwise
    """
    count, _unit, target = speech_budget(text, duration)
    if target is None:
        return True

    min_units, max_units = target
    return min_units <= count <= max_units


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


BRIEF_HEADER = "【赛道爆款简报 — 最高优先级，覆盖下方通用风格规则】"
BRIEF_FOOTER = (
    "以上简报优先于通用风格规则：简报要求的钩子 / 节奏 / 信息缺口 / CTA 必须落地，"
    "不得因「松弛自然」而弱化或省略。"
)


def _bullets(items: Any) -> List[str]:
    """把列表渲染成 '- xxx' 行；None / 空串条目跳过。"""
    return [f"- {str(x).strip()}" for x in (items or []) if str(x or "").strip()]


def render_brief_section(brief: Optional[Dict[str, Any]]) -> str:
    """把 creation brief 渲染成一段可注入 screenwriter 的中文指令；None → 空串。

    这里必须尽量保留简报里「具体、可模仿」的内容 —— 钩子原句、do / dont 清单、
    赛道共性。只喂 common_hook_types 这类抽象标签，编剧会退回写套话（历史 bug：
    513 字的简报被渲染成 104 字标签，产出的逐字稿因此空洞）。
    """
    if not brief:
        return ""
    hook = brief.get("hook_strategy") or {}
    struct = brief.get("script_structure") or {}
    directives = (brief.get("screenwriter_directives") or "").strip()
    niche = (brief.get("niche_summary") or "").strip()
    hook_types = [t for t in (hook.get("common_hook_types") or []) if t]
    example_hooks = hook.get("example_hooks") or []
    do_items = brief.get("do") or []
    dont_items = brief.get("dont") or []
    pacing = struct.get("pacing") or ""
    emotion = struct.get("emotion") or ""
    info_gap = struct.get("info_gap") or ""
    cta = struct.get("cta") or ""

    lines: List[str] = [BRIEF_HEADER, directives]

    if niche:
        lines.append(f"赛道共性：{niche}")
    if hook_types:
        lines.append(f"常见钩子类型：{'、'.join(hook_types)}")
    if _bullets(example_hooks):
        lines.append("爆款钩子原句（学它的说话方式和信息密度，换成本片主题，不要照抄）：")
        lines.extend(_bullets(example_hooks))

    struct_bits: List[str] = []
    if pacing and emotion:
        struct_bits.append(f"节奏/情绪：{pacing} / {emotion}")
    elif pacing or emotion:
        struct_bits.append(f"节奏/情绪：{pacing or emotion}")
    if info_gap:
        struct_bits.append(f"信息缺口：{info_gap}")
    if cta:
        struct_bits.append(f"CTA：{cta}")
    if struct_bits:
        lines.append("；".join(struct_bits))

    if _bullets(do_items):
        lines.append("必须做到：")
        lines.extend(_bullets(do_items))
    if _bullets(dont_items):
        lines.append("绝对避免：")
        lines.extend(_bullets(dont_items))

    lines.append(BRIEF_FOOTER)
    return "\n".join(x for x in lines if x)


async def run_screenwriter(
    theme_text: str,
    reference_images: List[Dict[str, Any]],
    llm_provider: GeminiProvider,
    aspect_ratio: str = "16:9",
    creation_brief: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Generate storyboard from theme and reference images.

    Args:
        theme_text: User's theme/description
        reference_images: List of reference image dicts with 'kind', 'path', 'filename'
        llm_provider: Gemini provider instance
        aspect_ratio: Video aspect ratio (e.g., "16:9", "9:16")
        creation_brief: Optional creation brief dict snapshotted on the project

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

    # 简报放在最后：它是最高优先级的创作约束，末位比夹在参考图和主题之间更吃权重。
    brief_text = render_brief_section(creation_brief)
    if brief_text:
        user_parts.append({"type": "text", "data": brief_text})

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

        count, unit, target = speech_budget(text, duration)
        if not validate_word_count(text, duration):
            word_count_warnings.append(
                {
                    "shot_id": shot["shot_id"],
                    "text_length": len(text),
                    "actual": count,
                    "unit": unit,
                    "duration": duration,
                    "recommended": target,
                }
            )
            shot["word_count_warning"] = True
        else:
            shot["word_count_warning"] = False

    return {
        "storyboard": result,
        "word_count_warnings": word_count_warnings,
    }
