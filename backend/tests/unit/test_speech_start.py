import shutil
import pytest
from pathlib import Path
from ffmpeg import FFmpeg
from app.agents.video_trimmer import detect_speech_start

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not found")


def _lead_silence_then_tone(path: Path, silence: float = 1.0, tone: float = 1.0):
    # 前 silence 秒静音，再 tone 秒 440Hz
    (FFmpeg().option("y")
     .input(f"anullsrc=r=44100:cl=mono:d={silence}", f="lavfi")
     .input(f"sine=frequency=440:duration={tone}", f="lavfi")
     .option("filter_complex", "[0][1]concat=n=2:v=0:a=1[a]")
     .output(str(path), map="[a]", acodec="pcm_s16le")
    ).execute()


def test_detect_speech_start_finds_lead_silence(tmp_path):
    p = tmp_path / "a.wav"; _lead_silence_then_tone(p, 1.0, 1.0)
    start = detect_speech_start(str(p))
    assert start is not None
    assert 0.7 < start < 1.3, f"起始应≈1.0s，实际 {start}"


def test_detect_speech_start_none_when_no_lead_silence(tmp_path):
    p = tmp_path / "b.wav"
    (FFmpeg().option("y").input("sine=frequency=440:duration=1.0", f="lavfi")
     .output(str(p), acodec="pcm_s16le")).execute()
    assert detect_speech_start(str(p)) in (None, 0.0) or detect_speech_start(str(p)) < 0.2
