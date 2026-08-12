import pytest
from app.agents.screenwriter import (
    BRIEF_FOOTER,
    BRIEF_HEADER,
    render_brief_section,
    run_screenwriter,
)

BRIEF = {
    "niche_summary": "美妆快节奏教程",
    "hook_strategy": {"common_hook_types": ["悬念"], "example_hooks": ["wait for it"]},
    "script_structure": {"pacing": "快", "emotion": "正向", "info_gap": "制造", "cta": "引导评论"},
    "do": ["前3秒点名人群"],
    "dont": ["不要自我介绍"],
    "screenwriter_directives": "开场0秒抛悬念钩子，语速偏快。",
}


def test_render_brief_section_contains_directives_and_hooks():
    text = render_brief_section(BRIEF)
    assert "开场0秒抛悬念钩子" in text
    assert "悬念" in text          # hook 类型
    assert "引导评论" in text      # cta


def test_render_brief_section_empty_when_none():
    assert render_brief_section(None) == ""


def test_render_brief_section_keeps_concrete_material():
    """简报里最具体的部分（钩子原句 / do / dont / 赛道共性）必须逐字进提示词。

    回归：旧实现只注入抽象标签，把 example_hooks / do / dont / niche_summary 全丢了，
    编剧因此退回写套话。
    """
    text = render_brief_section(BRIEF)
    assert "wait for it" in text        # 钩子原句
    assert "前3秒点名人群" in text       # do
    assert "不要自我介绍" in text        # dont
    assert "美妆快节奏教程" in text      # 赛道共性


def test_render_brief_section_matches_exact_expected_format():
    """防止渲染格式漂移：字段齐全时的输出必须逐字符一致。"""
    text = render_brief_section(BRIEF)
    expected = (
        f"{BRIEF_HEADER}\n"
        "开场0秒抛悬念钩子，语速偏快。\n"
        "赛道共性：美妆快节奏教程\n"
        "常见钩子类型：悬念\n"
        "爆款钩子原句（学它的说话方式和信息密度，换成本片主题，不要照抄）：\n"
        "- wait for it\n"
        "节奏/情绪：快 / 正向；信息缺口：制造；CTA：引导评论\n"
        "必须做到：\n"
        "- 前3秒点名人群\n"
        "绝对避免：\n"
        "- 不要自我介绍\n"
        f"{BRIEF_FOOTER}"
    )
    assert text == expected


def test_render_brief_section_declares_priority_over_style_rules():
    """简报必须自带「优先级高于通用风格规则」的声明，否则会被 system prompt 的
    'No hard sell / no algorithm-chasing hooks' 压掉。"""
    text = render_brief_section(BRIEF)
    assert "最高优先级" in text
    assert "优先于通用风格规则" in text


def test_render_brief_section_handles_explicit_none_nested_values():
    """screenwriter_directives / common_hook_types / pacing 等嵌套字段显式为 None 时
    不应抛异常，也不应把字面 "None" 渲染进提示词。"""
    brief = {
        "niche_summary": None,
        "hook_strategy": {"common_hook_types": None, "example_hooks": None},
        "script_structure": {"pacing": None, "emotion": None, "info_gap": None, "cta": None},
        "do": None,
        "dont": None,
        "screenwriter_directives": None,
    }
    text = render_brief_section(brief)
    assert "None" not in text
    assert BRIEF_HEADER in text


def test_render_brief_section_skips_empty_list_entries():
    """列表里的空串 / None 条目不应渲染成空的 '- ' 行。"""
    brief = {
        "hook_strategy": {"example_hooks": ["真钩子", "", None]},
        "do": ["", None],
    }
    text = render_brief_section(brief)
    assert "- 真钩子" in text
    assert "- \n" not in text
    assert "必须做到：" not in text     # do 全空 → 整段不出现


class _CaptureProvider:
    def __init__(self, shots=None):
        self._shots = shots or [
            {"shot_id": 1, "text": "hi there friend", "shot_type": "Close-up",
             "visual_description": "d", "shot_duration": 4, "align_with_previous": False}
        ]

    async def generate_json(self, *, model, system_prompt, user_parts,
                            response_schema, temperature=0.7, operation=None):
        self.text_parts = [p["data"] for p in user_parts if p["type"] == "text"]
        self.captured = " ".join(self.text_parts)
        return {"scene_overview": "ov", "shots": self._shots}


async def test_run_screenwriter_injects_brief_into_prompt():
    prov = _CaptureProvider()
    await run_screenwriter("主题X", [], prov, "9:16", creation_brief=BRIEF)
    assert "开场0秒抛悬念钩子" in prov.captured


async def test_run_screenwriter_puts_brief_last():
    """简报是最高优先级约束，必须排在主题 / 画面比例之后的末位。"""
    prov = _CaptureProvider()
    await run_screenwriter("主题X", [], prov, "9:16", creation_brief=BRIEF)
    assert prov.text_parts[-1].startswith(BRIEF_HEADER)
    assert any(p.startswith("主题：") for p in prov.text_parts[:-1])


async def test_run_screenwriter_without_brief_appends_nothing():
    prov = _CaptureProvider()
    await run_screenwriter("主题X", [], prov, "9:16")
    assert BRIEF_HEADER not in prov.captured
    assert prov.text_parts[-1].startswith("画面比例：")
