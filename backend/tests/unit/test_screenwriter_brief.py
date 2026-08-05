import pytest
from app.agents.screenwriter import render_brief_section, run_screenwriter

BRIEF = {
    "niche_summary": "美妆快节奏教程",
    "hook_strategy": {"common_hook_types": ["悬念"], "example_hooks": ["wait for it"]},
    "script_structure": {"pacing": "快", "emotion": "正向", "info_gap": "制造", "cta": "引导评论"},
    "screenwriter_directives": "开场0秒抛悬念钩子，语速偏快。",
}


def test_render_brief_section_contains_directives_and_hooks():
    text = render_brief_section(BRIEF)
    assert "开场0秒抛悬念钩子" in text
    assert "悬念" in text          # hook 类型
    assert "引导评论" in text      # cta


def test_render_brief_section_empty_when_none():
    assert render_brief_section(None) == ""


def test_render_brief_section_matches_exact_expected_format():
    """防止渲染格式漂移：字段齐全时的输出必须逐字符一致。"""
    text = render_brief_section(BRIEF)
    expected = (
        "【赛道爆款简报 — 务必据此创作】\n"
        "开场0秒抛悬念钩子，语速偏快。\n"
        "常见钩子类型：悬念\n"
        "节奏/情绪：快 / 正向；信息缺口：制造；CTA：引导评论"
    )
    assert text == expected


def test_render_brief_section_handles_explicit_none_nested_values():
    """screenwriter_directives / common_hook_types / pacing 等嵌套字段显式为 None 时
    不应抛异常，也不应把字面 "None" 渲染进提示词。"""
    brief = {
        "hook_strategy": {"common_hook_types": None},
        "script_structure": {"pacing": None, "emotion": None, "info_gap": None, "cta": None},
        "screenwriter_directives": None,
    }
    text = render_brief_section(brief)
    assert "None" not in text
    assert "【赛道爆款简报 — 务必据此创作】" in text


class _CaptureProvider:
    async def generate_json(self, *, model, system_prompt, user_parts,
                            response_schema, temperature=0.7, operation=None):
        self.captured = " ".join(p["data"] for p in user_parts if p["type"] == "text")
        return {"scene_overview": "ov", "shots": [
            {"shot_id": 1, "text": "hi there friend", "shot_type": "Close-up",
             "visual_description": "d", "shot_duration": 4, "align_with_previous": False}
        ]}


async def test_run_screenwriter_injects_brief_into_prompt():
    prov = _CaptureProvider()
    await run_screenwriter("主题X", [], prov, "9:16", creation_brief=BRIEF)
    assert "开场0秒抛悬念钩子" in prov.captured
