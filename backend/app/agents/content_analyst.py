"""跨爆款样本的转写文本联合归纳 → creation brief（无对照、无结构化打标）。"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class SampleStats(BaseModel):
    sample_n: int
    no_speech_pct: float
    sample_warning: Optional[str] = None


class HookStrategy(BaseModel):
    common_hook_types: List[str]
    example_hooks: List[str]


class ScriptStructure(BaseModel):
    pacing: str
    emotion: str
    info_gap: str
    cta: str


class CreationBrief(BaseModel):
    niche_summary: str
    sample_stats: SampleStats
    hook_strategy: HookStrategy
    script_structure: ScriptStructure
    do: List[str]
    dont: List[str]
    screenwriter_directives: str


SYSTEM_PROMPT = """你是短视频爆款内容分析师。下面是若干条「爆款视频」的口播转写文本（含前3秒钩子 hook）。
请通读所有样本，联合归纳它们的共性制胜模式，输出一份可直接指导创作的简报。

要求：
- 只依据给出的口播文本归纳，不要臆造数据。
- 每条结论面向「怎么写下一条」，可执行。
- screenwriter_directives 写成一段可直接下发给编剧的中文创作指令。
- 严格输出 schema 指定的纯 JSON，无任何解释性文字。
注意：sample_stats 由系统另行填充，你填的 sample_stats 会被覆盖，可填占位值。"""


def build_user_parts(samples: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    parts: List[Dict[str, Any]] = []
    for i, s in enumerate(samples, 1):
        hook = (s.get("hook_text") or "").strip()
        full = (s.get("full_transcript") or "").strip()
        parts.append({
            "type": "text",
            "data": f"样本 {i}\n【前3秒钩子】{hook}\n【完整口播】{full}",
        })
    return parts


async def run_content_analysis_brief(
    samples: List[Dict[str, str]], provider, model: str
) -> Dict[str, Any]:
    return await provider.generate_json(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        user_parts=build_user_parts(samples),
        response_schema=CreationBrief,
        temperature=0.4,
        operation="agents-content-analyst-brief",
    )
