import os

import pytest
from app.services import storage
from app.services.storage import get_original_video_for_audio, shot_dir


@pytest.fixture
def shot_path(tmp_path, monkeypatch):
    monkeypatch.setattr(storage.settings, "storage_root", str(tmp_path))
    d = shot_dir("proj", 1)
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_returns_newest_unique_when_no_pre_vc(shot_path):
    old = shot_path / "output_1000_aaaa.mp4"
    new = shot_path / "output_2000_bbbb.mp4"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    assert get_original_video_for_audio("proj", 1).name == "output_2000_bbbb.mp4"


def test_excludes_vc_output(shot_path):
    """vc_* files are not output_*.mp4; source must be the output_ file."""
    vc = shot_path / "vc_1000_aaaa.mp4"
    src = shot_path / "output_2000_bbbb.mp4"
    vc.write_bytes(b"vc")
    src.write_bytes(b"src")
    os.utime(vc, (3000, 3000))   # vc is newer by mtime — must still be excluded
    os.utime(src, (2000, 2000))
    assert get_original_video_for_audio("proj", 1).name == "output_2000_bbbb.mp4"


def test_raises_when_no_video(shot_path):
    with pytest.raises(FileNotFoundError):
        get_original_video_for_audio("proj", 1)
