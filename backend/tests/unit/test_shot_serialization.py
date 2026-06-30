from app.api.projects import _shot_to_dict
from app.config import settings
from app.models.project import Shot


def test_shot_to_dict_includes_playback_descriptor(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    vc_audio = tmp_path / "projects" / "p" / "shots" / "shot_1" / "audio_vc_1_ab.wav"
    vc_audio.parent.mkdir(parents=True)
    vc_audio.touch()
    s = Shot(
        project_id="p", shot_id=1, text="hi", shot_type="Close-up",
        visual_description="x", shot_duration=4, status="completed",
        trim_frames=60, source_fps=30.0, source_frames=120,
        vc_audio_path=str(vc_audio),
    )
    d = _shot_to_dict(s)
    assert d["trim_frames"] == 60
    assert d["source_frames"] == 120
    assert abs(d["trim_end_sec"] - 2.0) < 1e-6      # 60 / 30
    assert d["vc_audio_url"].startswith("/api/media/")


def test_trim_end_sec_none_when_no_trim():
    s = Shot(project_id="p", shot_id=1, text="t", shot_type="x",
             visual_description="x", shot_duration=4, status="completed",
             trim_frames=None, source_fps=30.0, source_frames=120)
    assert _shot_to_dict(s)["trim_end_sec"] is None
