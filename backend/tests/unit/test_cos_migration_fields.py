"""fields.py 的纯逻辑测试——不碰 COS、不碰 DB，无凭证环境必须照跑。"""
import json

import pytest

from app.scripts.cos_migration.fields import (
    ALREADY_KEY, LEGACY_ABS, LEGACY_REL, UNRECOGNIZED,
    FIELDS, classify, rewrite_json_dict_of_lists, rewrite_json_list, to_key,
)


@pytest.mark.parametrize("value,expected", [
    # 真实 dev DB 里 346/349 个值长这样——注意它不以 / 开头
    ("storage/projects/abc/shots/shot_1/output.mp4", LEGACY_REL),
    ("storage/analyses/xyz/samples/1/source.mp4", LEGACY_REL),
    # Spec A 上线后新写入的值已经是 key
    ("projects/abc/storyboard.json", ALREADY_KEY),
    ("analyses/xyz/samples/1/source.mp4", ALREADY_KEY),
    # 防御性：万一某个库里存的是绝对路径
    ("/app/storage/projects/abc/shots/shot_1/output.mp4", LEGACY_ABS),
    # 认不出来的一律不碰
    ("", UNRECOGNIZED),
    ("some/other/thing.png", UNRECOGNIZED),
    ("/etc/passwd", UNRECOGNIZED),
])
def test_classify(value, expected):
    assert classify(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("storage/projects/abc/shots/shot_1/output.mp4", "projects/abc/shots/shot_1/output.mp4"),
    ("storage/analyses/xyz/samples/1/source.mp4", "analyses/xyz/samples/1/source.mp4"),
    ("/app/storage/projects/abc/x.png", "projects/abc/x.png"),
    # 幂等：已经是 key 的原样返回
    ("projects/abc/storyboard.json", "projects/abc/storyboard.json"),
])
def test_to_key(value, expected):
    assert to_key(value) == expected


def test_to_key_is_idempotent():
    """连跑两次必须收敛——这是 Spec B §2.1 的验收标准在纯逻辑层的体现。"""
    once = to_key("storage/projects/abc/shots/shot_1/output.mp4")
    assert to_key(once) == once


def test_to_key_rejects_unrecognized():
    with pytest.raises(ValueError):
        to_key("some/other/thing.png")


def test_rewrite_json_list():
    raw = json.dumps([
        "storage/projects/p/shots/shot_3/custom_frames/a.jpg",
        "storage/projects/p/shots/shot_3/custom_frames/b.jpg",
    ])
    out = rewrite_json_list(raw)
    assert json.loads(out) == [
        "projects/p/shots/shot_3/custom_frames/a.jpg",
        "projects/p/shots/shot_3/custom_frames/b.jpg",
    ]
    # 幂等：已转换过的返回 None（无变化）
    assert rewrite_json_list(out) is None


def test_rewrite_json_dict_of_lists():
    raw = json.dumps({
        "character": ["storage/projects/p/reference_images/c.jpg"],
        "object": [],
    })
    out = rewrite_json_dict_of_lists(raw)
    assert json.loads(out) == {
        "character": ["projects/p/reference_images/c.jpg"],
        "object": [],
    }
    assert rewrite_json_dict_of_lists(out) is None


def test_rewrite_json_handles_empty_and_garbage():
    for raw in ("", "[]", "null", "not json"):
        assert rewrite_json_list(raw) is None


def test_field_registry_covers_every_known_path_column():
    """字段漏一个 = 该类资源全部失效（Spec B §9.1）。这里把实测确认过的
    完整清单钉死，将来加字段必须同步改这个断言。"""
    got = {(f.table, f.column) for f in FIELDS}
    assert got == {
        ("shots", "video_path"),
        ("shots", "last_frame_path"),
        ("shots", "custom_first_frame_path"),
        ("shots", "target_last_frame_path"),
        ("shots", "vc_audio_path"),
        ("shots", "custom_reference_paths"),
        ("projects", "storyboard_path"),
        ("projects", "final_video_path"),
        ("projects", "reference_voice_path"),
        ("reference_images", "storage_path"),
        ("image_candidates", "file_path"),
        ("image_candidates", "ref_paths"),
        ("reference_samples", "video_path"),
        ("reference_samples", "audio_path"),
    }


def test_reference_samples_is_marked_optional():
    """ReferenceSample 模型来自未合并的草稿 PR #38，本分支没有——
    必须靠建表检测保护，不能硬 import。"""
    opt = {(f.table, f.column) for f in FIELDS if f.optional_table}
    assert opt == {("reference_samples", "video_path"), ("reference_samples", "audio_path")}


def test_json_fields_are_marked_with_right_kind():
    kinds = {(f.table, f.column): f.kind for f in FIELDS}
    assert kinds[("shots", "custom_reference_paths")] == "json_list"
    assert kinds[("image_candidates", "ref_paths")] == "json_dict_of_lists"


def test_classify_rejects_malformed_double_slash():
    """Double slash after storage/ prefix: result would be /projects/..., not a valid key."""
    assert classify("storage//projects/x.png") == UNRECOGNIZED


def test_classify_rejects_prefix_only():
    """storage/ with no suffix → empty candidate after stripping."""
    assert classify("storage/") == UNRECOGNIZED


def test_classify_rejects_nested_storage_marker():
    """Path with /storage/ appearing twice: first split yields storage/..., not a valid key."""
    assert classify("/app/storage/storage/projects/x.png") == UNRECOGNIZED


def test_to_key_idempotency_comprehensive():
    """to_key(to_key(v)) converges for all valid inputs; consistently rejects invalid ones."""
    # Valid inputs: legacy → key conversion
    legacy_inputs = [
        ("storage/projects/abc/shots/shot_1/output.mp4", "projects/abc/shots/shot_1/output.mp4"),
        ("storage/analyses/xyz/samples/1/source.mp4", "analyses/xyz/samples/1/source.mp4"),
        ("/app/storage/projects/abc/x.png", "projects/abc/x.png"),
    ]
    for input_val, expected_key in legacy_inputs:
        once = to_key(input_val)
        assert once == expected_key
        twice = to_key(once)
        assert twice == once == expected_key

    # Already-key inputs: identity
    key_inputs = [
        "projects/abc/storyboard.json",
        "analyses/xyz/samples/1/source.mp4",
    ]
    for key_val in key_inputs:
        once = to_key(key_val)
        twice = to_key(once)
        assert once == twice == key_val

    # Invalid/malformed inputs: must fail consistently
    invalid_inputs = [
        "storage//projects/x.png",  # double slash
        "storage/",  # prefix only
        "/app/storage/storage/projects/x.png",  # nested /storage/
        "some/other/thing.png",  # unrecognized
    ]
    for input_val in invalid_inputs:
        with pytest.raises(ValueError):
            to_key(input_val)
