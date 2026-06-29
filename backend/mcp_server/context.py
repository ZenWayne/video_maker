from mcp_server.validation import word_count_report


def shape_project(p: dict) -> dict:
    characters = [
        {"filename": r["filename"], "kind": r["kind"]}
        for r in p.get("reference_images", [])
        if r.get("kind") == "character"
    ]
    return {
        "id": p["id"],
        "theme": p.get("theme_text"),
        "status": p["status"],
        "aspect_ratio": p.get("aspect_ratio"),
        "scene_overview": p.get("scene_overview"),
        "characters": characters,
        "shot_count": len(p.get("shots", [])),
    }


def shape_shot(shot: dict) -> dict:
    wc = word_count_report(shot.get("text") or "", shot["shot_duration"])
    return {
        "shot_id": shot["shot_id"],
        "order_index": shot["shot_id"],  # shot_id is the ordering key
        "shot_type": shot["shot_type"],
        "shot_duration": shot["shot_duration"],
        "align_with_previous": shot["align_with_previous"],
        "text": shot.get("text"),
        "motion_prompt": shot.get("motion_prompt"),
        "visual_description": shot.get("visual_description"),
        "word_count": wc["actual"],
        "word_count_target": wc["target_range"],
        "has_video": bool(shot.get("video_path")),
    }


def with_neighbors(shots: list[dict], shot_id: int) -> dict:
    ordered = sorted(shots, key=lambda s: s["shot_id"])
    idx = next((i for i, s in enumerate(ordered) if s["shot_id"] == shot_id), None)
    if idx is None:
        raise KeyError(f"shot {shot_id} not found")
    shaped = shape_shot(ordered[idx])
    shaped["prev_text"] = ordered[idx - 1].get("text") if idx > 0 else None
    shaped["next_text"] = ordered[idx + 1].get("text") if idx < len(ordered) - 1 else None
    return shaped
