"""逐字稿长度校验的语言自适应口径（中文按字、英文按词）。"""

from app.agents.screenwriter import (
    CJK_CHAR_COUNT_RULES,
    WORD_COUNT_RULES,
    count_speech_units,
    speech_budget,
    validate_word_count,
)


def test_english_counted_by_words():
    assert count_speech_units("one two three four") == (4, "words")


def test_chinese_counted_by_characters_not_split():
    """回归：str.split() 对中文恒为 1，旧实现让中文逐字稿永远「通过」校验。"""
    text = "你最近老是梦到他，先别急着联系。"
    assert len(text.split()) == 1
    count, unit = count_speech_units(text)
    assert unit == "cjk_chars"
    assert count == 14  # 「你最近老是梦到他」8 + 「先别急着联系」6，标点不计


def test_mixed_text_dominated_by_chinese_uses_char_rules():
    count, unit = count_speech_units("今天聊聊 AI agent 怎么用")
    assert unit == "cjk_chars"
    assert count == 7  # 今天聊聊 4 + 怎么用 3；拉丁词不计入字数


def test_empty_text_falls_back_to_words():
    assert count_speech_units("") == (0, "words")
    assert count_speech_units("   ") == (0, "words")


def test_chinese_uses_cjk_table_not_english_table():
    text = "字" * 36  # 8s 中文预算 32–40 字
    count, unit, target = speech_budget(text, 8)
    assert (count, unit, target) == (36, "cjk_chars", CJK_CHAR_COUNT_RULES[8])
    assert validate_word_count(text, 8) is True
    # 同样长度按英文词表（18–21）会被判超长 —— 证明两张表确实分开了
    assert not (WORD_COUNT_RULES[8][0] <= 36 <= WORD_COUNT_RULES[8][1])


def test_chinese_too_short_is_flagged():
    """8s 只写 20 字 ≈ 一半时长的内容量，正是「空洞」的形态，必须告警。"""
    assert validate_word_count("字" * 20, 8) is False


def test_chinese_too_long_is_flagged():
    assert validate_word_count("字" * 60, 8) is False


def test_english_ranges_unchanged():
    assert validate_word_count("one two three four five six seven eight", 4) is True
    assert validate_word_count("too short", 8) is False


def test_unknown_duration_always_passes():
    count, unit, target = speech_budget("whatever text here", 5)
    assert target is None
    assert validate_word_count("whatever text here", 5) is True
