import subprocess
from pathlib import Path
import pytest
from app.services.reference_voice import (
    has_audio_stream, normalize_reference_voice,
)


def _make_tone(path: str, fmt_args: list[str]):
    # 0.5s 440Hz sine → container chosen by extension
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
         *fmt_args, path],
        check=True, capture_output=True,
    )


@pytest.mark.parametrize("name,args", [
    ("in.wav", []),
    ("in.m4a", ["-c:a", "aac"]),
    ("in.mp4", ["-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.5", "-shortest"]),
])
def test_normalize_outputs_mono_16k_wav(tmp_path, name, args):
    src = str(tmp_path / name)
    if name == "in.mp4":
        # build an mp4 with both video + the sine audio
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
             "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.5",
             "-shortest", src],
            check=True, capture_output=True,
        )
    else:
        _make_tone(src, args)
    out = str(tmp_path / "prompt.wav")
    assert normalize_reference_voice(src, out) == out
    assert Path(out).exists()
    # verify sample rate + channels via ffprobe
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate,channels", "-of", "csv=p=0", out],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert probe == "16000,1"


def test_has_audio_stream_true_false(tmp_path):
    wav = str(tmp_path / "a.wav")
    _make_tone(wav, [])
    assert has_audio_stream(wav) is True
    silent_mp4 = str(tmp_path / "v.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.5", silent_mp4],
        check=True, capture_output=True,
    )
    assert has_audio_stream(silent_mp4) is False
