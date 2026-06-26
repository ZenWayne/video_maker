import pytest
from app.services import storage
from app.services.storage import get_original_video_for_audio, shot_dir


@pytest.fixture
def shot_path(tmp_path, monkeypatch):
    monkeypatch.setattr(storage.settings, "storage_root", str(tmp_path))
    d = shot_dir("proj", 1)
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_prefers_pre_vc_backup(shot_path):
    (shot_path / "output_pre_vc.mp4").write_bytes(b"prevc")
    (shot_path / "output_1000_aaaa.mp4").write_bytes(b"current")
    assert get_original_video_for_audio("proj", 1).name == "output_pre_vc.mp4"


def test_returns_newest_unique_when_no_pre_vc(shot_path):
    old = shot_path / "output_1000_aaaa.mp4"
    new = shot_path / "output_2000_bbbb.mp4"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    import os
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    assert get_original_video_for_audio("proj", 1).name == "output_2000_bbbb.mp4"


def test_excludes_trim_backup(shot_path):
    (shot_path / "output_original.mp4").write_bytes(b"pretrim")
    (shot_path / "output_3000_cccc.mp4").write_bytes(b"current")
    assert get_original_video_for_audio("proj", 1).name == "output_3000_cccc.mp4"


def test_raises_when_no_video(shot_path):
    with pytest.raises(FileNotFoundError):
        get_original_video_for_audio("proj", 1)
