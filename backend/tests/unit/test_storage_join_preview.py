from pathlib import Path

from app.config import settings
from app.services import storage


def test_join_preview_path(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))

    p = storage.join_preview_path("proj-123")

    expected = tmp_path / "projects" / "proj-123" / "previews" / "join_preview.mp4"
    assert Path(p) == expected
    # parent dir is created so ffmpeg can write into it
    assert expected.parent.is_dir()
