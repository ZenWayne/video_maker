"""key 拼接纯函数——无网络，最快的一层。"""
import re

from app.services import storage


def test_project_prefix():
    assert storage.project_prefix("p1") == "projects/p1/"


def test_shot_prefix():
    assert storage.shot_prefix("p1", 3) == "projects/p1/shots/shot_3/"


def test_shot_key_joins_filename():
    assert storage.shot_key("p1", 3, "output_1.mp4") == \
        "projects/p1/shots/shot_3/output_1.mp4"


def test_fixed_name_keys():
    assert storage.shot_audio_original_key("p1", 2) == \
        "projects/p1/shots/shot_2/audio_original.wav"
    assert storage.shot_audio_vc_key("p1", 2) == \
        "projects/p1/shots/shot_2/audio_vc.wav"
    assert storage.shot_target_last_frame_key("p1", 2) == \
        "projects/p1/shots/shot_2/target_last_frame.png"


def test_project_level_keys():
    assert storage.storyboard_key("p1") == "projects/p1/storyboard.json"
    assert storage.final_video_key("p1") == "projects/p1/final/merged.mp4"
    assert storage.join_preview_key("p1") == "projects/p1/previews/join_preview.mp4"
    assert storage.archived_storyboard_key("p1", "20260726") == \
        "projects/p1/storyboard_20260726.json"


def test_reference_image_key():
    assert storage.reference_image_key("p1", "img7", "face.jpg") == \
        "projects/p1/reference_images/img7_face.jpg"


def test_candidates_and_custom_frames_prefixes():
    assert storage.shot_candidates_prefix("p1", 4) == \
        "projects/p1/shots/shot_4/candidates/"
    assert storage.shot_custom_frames_prefix("p1", 4) == \
        "projects/p1/shots/shot_4/custom_frames/"


def test_no_key_has_leading_slash():
    """DB 存裸 key——前导斜杠会让 key 与迁移脚本的相对路径映射对不上。"""
    keys = [
        storage.project_prefix("p1"),
        storage.shot_prefix("p1", 1),
        storage.storyboard_key("p1"),
        storage.final_video_key("p1"),
        storage.reference_image_key("p1", "i", "f.jpg"),
    ]
    assert all(not k.startswith("/") for k in keys)


def test_ts_uuid_name_is_unique_and_well_formed():
    a = storage.ts_uuid_name(".mp4")
    b = storage.ts_uuid_name(".mp4")
    assert a != b
    assert re.fullmatch(r"\d+_[0-9a-f]{8}\.mp4", a)


def test_is_valid_key_rejects_traversal_and_absolute():
    assert storage.is_valid_key("projects/p1/shots/shot_1/output.mp4") is True
    assert storage.is_valid_key("projects/p1/../../etc/passwd") is False
    assert storage.is_valid_key("/projects/p1/x.mp4") is False
    assert storage.is_valid_key("etc/passwd") is False
    assert storage.is_valid_key("") is False


def test_to_media_url_passes_none_through():
    assert storage.to_media_url(None) is None
    assert storage.to_media_url("") is None
