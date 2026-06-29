import pytest
from app.models.project import Shot


def test_shot_has_edl_columns():
    cols = {c.name for c in Shot.__table__.columns}
    assert {"trim_frames", "source_fps", "source_frames", "vc_audio_path"} <= cols
