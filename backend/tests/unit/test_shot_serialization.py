from app.api.projects import _shot_to_dict
from app.models.project import Shot
from tests.integration.conftest import install_fake_cos_credentials


def test_shot_to_dict_includes_playback_descriptor(monkeypatch):
    install_fake_cos_credentials(monkeypatch)
    vc_audio_key = "projects/p/shots/shot_1/audio_vc_1_ab.wav"
    s = Shot(
        project_id="p", shot_id=1, text="hi", shot_type="Close-up",
        visual_description="x", shot_duration=4, status="completed",
        trim_frames=60, source_fps=30.0, source_frames=120,
        vc_audio_path=vc_audio_key,
    )
    d = _shot_to_dict(s)
    assert d["trim_frames"] == 60
    assert d["source_frames"] == 120
    assert abs(d["trim_end_sec"] - 2.0) < 1e-6      # 60 / 30
    assert d["vc_audio_url"].startswith("http")
    assert "/api/media/" not in d["vc_audio_url"]


def test_trim_end_sec_none_when_no_trim():
    s = Shot(project_id="p", shot_id=1, text="t", shot_type="x",
             visual_description="x", shot_duration=4, status="completed",
             trim_frames=None, source_fps=30.0, source_frames=120)
    assert _shot_to_dict(s)["trim_end_sec"] is None
