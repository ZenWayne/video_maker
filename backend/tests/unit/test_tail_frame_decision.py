"""Unit tests for resolve_tail_frame — path-presence-only decision."""
from pathlib import Path
import worker.tasks as tasks


def test_tail_used_when_path_present(tmp_path):
    f = tmp_path / "t.png"; f.write_bytes(b"x")
    assert tasks.resolve_tail_frame(str(f)) == str(f)


def test_tail_none_when_path_empty():
    assert tasks.resolve_tail_frame(None) is None


def test_tail_none_when_file_missing(tmp_path):
    assert tasks.resolve_tail_frame(str(tmp_path / "missing.png")) is None
