"""Unit tests for video_trimmer — speech-end fallback trim (no tail frame).

ffmpeg/ffprobe are mocked; we test the orchestration (speech-end → frame math →
trim point), not real video processing.
"""
from unittest.mock import patch

import app.agents.video_trimmer as vt


def test_auto_trim_to_speech_end_trims_at_speech_end():
    """Keep frames up to speech end + a small tail window, drop the rest."""
    with patch.object(vt, "detect_speech_end", return_value=2.0), \
         patch.object(vt, "get_video_info", return_value={"fps": 25.0, "total_frames": 200, "duration": 8.0}), \
         patch.object(vt, "_backup_and_trim", return_value={"trimmed_to_frame": 52}) as m_trim:
        result = vt.auto_trim_to_speech_end("/fake/out.mp4")

    # 2.0s * 25fps = 50, + window int(0.1*25)=2 → keep 52 frames
    m_trim.assert_called_once_with("/fake/out.mp4", 52)
    assert result == {"trimmed_to_frame": 52}


def test_auto_trim_to_speech_end_no_trailing_silence_returns_none():
    """No trailing silence (e.g. no dialogue) → nothing to trim."""
    with patch.object(vt, "detect_speech_end", return_value=None), \
         patch.object(vt, "_backup_and_trim") as m_trim:
        result = vt.auto_trim_to_speech_end("/fake/out.mp4")

    assert result is None
    m_trim.assert_not_called()


def test_auto_trim_to_speech_end_speech_runs_to_end_returns_none():
    """Speech end is at/after the last frame → no tail to trim."""
    with patch.object(vt, "detect_speech_end", return_value=7.99), \
         patch.object(vt, "get_video_info", return_value={"fps": 25.0, "total_frames": 200, "duration": 8.0}), \
         patch.object(vt, "_backup_and_trim") as m_trim:
        result = vt.auto_trim_to_speech_end("/fake/out.mp4")

    assert result is None
    m_trim.assert_not_called()


def test_suggest_silence_trim_returns_suggested_frame():
    """Trailing silence at 2.0s, 25fps → 50 + 3 padding = keep 53 frames."""
    with patch.object(vt, "detect_speech_end", return_value=2.0), \
         patch.object(vt, "get_video_info", return_value={"fps": 25.0, "total_frames": 200, "duration": 8.0}):
        result = vt.suggest_silence_trim("/fake/out.mp4")

    assert result == {
        "suggested_end_frame": 53,
        "silence_start_time": 2.0,
        "fps": 25.0,
        "total_frames": 200,
        "duration": 8.0,
    }


def test_suggest_silence_trim_no_trailing_silence_returns_none():
    """No trailing silence → nothing to suggest."""
    with patch.object(vt, "detect_speech_end", return_value=None):
        result = vt.suggest_silence_trim("/fake/out.mp4")

    assert result is None


def test_suggest_silence_trim_clamps_to_min_frames():
    """Suggested frame below the 24-frame floor is clamped up to 24."""
    with patch.object(vt, "detect_speech_end", return_value=0.2), \
         patch.object(vt, "get_video_info", return_value={"fps": 25.0, "total_frames": 200, "duration": 8.0}):
        result = vt.suggest_silence_trim("/fake/out.mp4")

    # 0.2*25=5, +3=8 → clamped up to 24
    assert result["suggested_end_frame"] == 24


def test_suggest_silence_trim_suggestion_at_or_past_end_returns_none():
    """Silence onset + padding reaches the last frame → nothing to trim."""
    with patch.object(vt, "detect_speech_end", return_value=7.95), \
         patch.object(vt, "get_video_info", return_value={"fps": 25.0, "total_frames": 200, "duration": 8.0}):
        result = vt.suggest_silence_trim("/fake/out.mp4")

    # 7.95*25=198.75→round 199, +3=202 >= 200 → None
    assert result is None


def test_suggest_silence_trim_tiny_video_clamp_then_bounds_returns_none():
    """Clamp-to-24 must still respect total_frames: 24 >= total → None."""
    with patch.object(vt, "detect_speech_end", return_value=0.2), \
         patch.object(vt, "get_video_info", return_value={"fps": 25.0, "total_frames": 20, "duration": 0.8}):
        result = vt.suggest_silence_trim("/fake/out.mp4")

    assert result is None
