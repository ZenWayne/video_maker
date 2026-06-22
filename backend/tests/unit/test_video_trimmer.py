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
