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
