"""内容分析（爆款归因）COS key 拼接纯函数——无网络，最快的一层。"""
from app.services import storage
from app.config import settings


def test_analysis_prefix():
    assert storage.analysis_prefix("A1") == "analyses/A1/"


def test_sample_prefix():
    assert storage.sample_prefix("A1", 7) == "analyses/A1/sample_7/"


def test_sample_video_key():
    assert storage.sample_video_key("A1", 7, "source_clip.mp4") == \
        "analyses/A1/sample_7/source_clip.mp4"


def test_asr_config_defaults():
    assert settings.asr_model == "large-v3"
    assert settings.asr_device == "cpu"
    assert settings.asr_compute_type == "int8"
    assert settings.content_analysis_model == "gemini-2.5-pro"
