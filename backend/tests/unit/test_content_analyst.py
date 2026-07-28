import pytest
from app.agents.content_analyst import (
    build_user_parts, run_content_analysis_brief, CreationBrief,
)

SAMPLES = [
    {"hook_text": "wait for it", "full_transcript": "wait for it, here is the trick"},
    {"hook_text": "3 mistakes", "full_transcript": "3 mistakes you make daily"},
]


def test_build_user_parts_includes_transcripts():
    parts = build_user_parts(SAMPLES)
    blob = " ".join(p["data"] for p in parts if p["type"] == "text")
    assert "wait for it" in blob
    assert "3 mistakes you make daily" in blob
    assert all(p["type"] == "text" for p in parts)  # 无 caption/图像


class _StubProvider:
    def __init__(self, canned):
        self.canned = canned
        self.seen = None

    async def generate_json(self, *, model, system_prompt, user_parts,
                            response_schema, temperature=0.7, operation=None):
        self.seen = {
            "model": model, "user_parts": user_parts, "operation": operation,
            "response_schema": response_schema, "temperature": temperature,
        }
        return self.canned


CANNED = {
    "niche_summary": "美妆快节奏教程",
    "sample_stats": {"sample_n": 2, "no_speech_pct": 0.0, "sample_warning": None},
    "hook_strategy": {"common_hook_types": ["悬念"], "example_hooks": ["wait for it"]},
    "script_structure": {"pacing": "快", "emotion": "正向", "info_gap": "制造", "cta": "引导评论"},
    "do": ["前3秒抛悬念"], "dont": ["平铺直叙"],
    "screenwriter_directives": "开场0秒抛出悬念钩子，语速偏快。",
}


async def test_run_brief_returns_validated_dict():
    prov = _StubProvider(CANNED)
    out = await run_content_analysis_brief(SAMPLES, prov, "gemini-2.5-pro")
    CreationBrief.model_validate(out)              # schema 合法
    assert out["screenwriter_directives"].startswith("开场")
    assert prov.seen["model"] == "gemini-2.5-pro"
    assert prov.seen["operation"] == "agents-content-analyst-brief"
    # 唯一一次计费 LLM 调用的配置断言：response_schema/temperature 是 JSON 模式
    # 生效的关键，掉了只会在真 Vertex 环境暴露（且带账单）——不能只靠人肉审查。
    assert prov.seen["response_schema"] is CreationBrief
    assert prov.seen["temperature"] == 0.4
