from app.agents.screenwriter import speech_budget, validate_word_count


def word_count_report(text: str, duration: int) -> dict:
    """Advisory word-count check; never blocks. Mirrors screenwriter rules.

    中文按字计、英文按词计，与 screenwriter 的校验口径保持一致。
    """
    actual, unit, target = speech_budget(text or "", duration)
    return {
        "actual": actual,
        "unit": unit,
        "target_range": list(target) if target else None,
        "within_range": validate_word_count(text or "", duration) if target else True,
    }
