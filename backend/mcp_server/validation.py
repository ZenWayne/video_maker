from app.agents.screenwriter import WORD_COUNT_RULES, validate_word_count


def word_count_report(text: str, duration: int) -> dict:
    """Advisory word-count check; never blocks. Mirrors screenwriter rules."""
    actual = len((text or "").strip().split())
    target = WORD_COUNT_RULES.get(duration)
    return {
        "actual": actual,
        "target_range": list(target) if target else None,
        "within_range": validate_word_count(text or "", duration) if target else True,
    }
