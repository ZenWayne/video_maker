from mcp_server.context import shape_project, shape_shot, with_neighbors


def _shot(i, **kw):
    base = dict(id=i, shot_id=i, text=f"line {i}", shot_type="Medium Shot",
                visual_description=f"v{i}", shot_duration=6, status="pending",
                align_with_previous=(i > 1), motion_prompt=None, video_path=None)
    base.update(kw)
    return base


def test_shape_project_filters_characters():
    p = {"id": "p1", "theme_text": "t", "status": "script_review", "aspect_ratio": "16:9",
         "scene_overview": "ov",
         "reference_images": [{"filename": "c.jpg", "kind": "character"},
                              {"filename": "s.jpg", "kind": "scene"}],
         "shots": [_shot(1), _shot(2)]}
    out = shape_project(p)
    assert out == {"id": "p1", "theme": "t", "status": "script_review",
                   "aspect_ratio": "16:9", "scene_overview": "ov",
                   "characters": [{"filename": "c.jpg", "kind": "character"}],
                   "shot_count": 2}


def test_shape_shot_word_count_and_has_video():
    out = shape_shot(_shot(1, text="a b c d", shot_duration=4, video_path="/x/output.mp4"))
    assert out["word_count"] == 4
    assert out["word_count_target"] == [8, 10]
    assert out["has_video"] is True
    assert out["motion_prompt"] is None


def test_with_neighbors():
    shots = [_shot(1, text="first"), _shot(2, text="second"), _shot(3, text="third")]
    out = with_neighbors(shots, 2)
    assert out["shot_id"] == 2
    assert out["prev_text"] == "first"
    assert out["next_text"] == "third"


def test_shape_generation_status():
    from mcp_server.context import shape_generation_status
    p = {
        "status": "shot_review",
        "shots": [
            {"shot_id": 2, "status": "pending", "video_path": None,
             "error_message": "boom", "vc_status": None, "tf_status": None},
            {"shot_id": 1, "status": "completed", "video_path": "projects/x/1/output.mp4",
             "error_message": None, "vc_status": "completed", "tf_status": None},
        ],
    }
    out = shape_generation_status(p)
    assert out["status"] == "shot_review"
    assert [s["shot_id"] for s in out["shots"]] == [1, 2]
    assert out["shots"][0] == {
        "shot_id": 1, "status": "completed", "has_video": True,
        "video_path": "projects/x/1/output.mp4", "error_message": None,
        "vc_status": "completed", "tf_status": None,
    }
    assert out["shots"][1]["has_video"] is False
    assert out["shots"][1]["error_message"] == "boom"
