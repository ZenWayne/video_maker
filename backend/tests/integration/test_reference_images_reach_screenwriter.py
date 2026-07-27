"""C2 regression: reference images must actually reach the screenwriter LLM call.

Bug (worker/tasks.run_screenwriter): ReferenceImage.storage_path is a COS key
(e.g. "projects/p1/reference_images/img_0.png"), but the old code did
``Path(img.storage_path).exists()`` to decide whether to attach it — a bare
filesystem check that is unconditionally False for a COS key. Every reference
image was therefore silently dropped before ever reaching the LLM: the
screenwriter built the whole storyboard with reference_images=[] regardless of
how many images the user actually uploaded, with no error and no log.

This test seeds N real ReferenceImage rows, mocks ONLY the model boundary
(GeminiProvider.generate_json — no real LLM call, no billing) and COS I/O
(object_store.exists/get — no real network/credentials), and lets the REAL
run_screenwriter (worker/tasks.py) + run_screenwriter agent (app/agents/
screenwriter.py) run. It asserts the LLM call actually received N image parts.
Must fail under the C2 bug (verified manually against the pre-fix code: with
the bare ``Path(key).exists()`` check, this assertion sees 0 image parts, not
N).
"""
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path as _Path

import pytest

import worker.tasks as tasks
from app.models.project import Project, ReferenceImage, ProjectStatus
from app.services import object_store

PID = "proj-c2-refs"
N_REF_IMAGES = 3

FAKE_STORYBOARD = {
    "scene_overview": "A quiet morning in the city.",
    "shots": [
        {
            "shot_id": 1,
            "shot_type": "Medium Shot",
            "visual_description": "A character wakes up.",
            "text": "早上好",
            "shot_duration": 4,
            "align_with_previous": False,
            "use_prev_last_frame": False,
            "word_count_warning": False,
        },
    ],
}


async def _seed(db_session_factory):
    async with db_session_factory() as s:
        s.add(Project(
            id=PID,
            title="t",
            theme_text="a story about mornings",
            creator_name="tester",
            status=ProjectStatus.SCRIPTING.value,
            aspect_ratio="9:16",
        ))
        for i in range(N_REF_IMAGES):
            s.add(ReferenceImage(
                project_id=PID,
                kind="character",
                filename=f"ref_{i}.png",
                storage_path=f"projects/{PID}/reference_images/ref_{i}.png",
                order_index=i,
            ))
        await s.commit()


def _fake_object_store(monkeypatch):
    """No real COS network/credentials: exists() is True for our seeded keys,
    get() materializes a tiny real PNG at the requested local path (workspace
    ``fetch()`` calls object_store.get(key, dest) under the hood)."""
    PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64

    async def fake_exists(key: str) -> bool:
        return key.startswith(f"projects/{PID}/reference_images/")

    async def fake_get(key: str, dest_path):
        dest_path = _Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(PNG_BYTES)
        return dest_path

    monkeypatch.setattr(object_store, "exists", fake_exists)
    monkeypatch.setattr(object_store, "get", fake_get)


@pytest.mark.asyncio
async def test_n_reference_images_reach_the_screenwriter_llm_call(
    db_session_factory, redis, monkeypatch
):
    await _seed(db_session_factory)
    _fake_object_store(monkeypatch)

    # Skip the (COS-writing) storyboard.json persistence — irrelevant to this
    # test and would otherwise need a real object_store.put() too.
    monkeypatch.setattr(tasks, "write_storyboard", AsyncMock(return_value="fake/key.json"))

    captured = {}

    async def fake_generate_json(**kwargs):
        # Must run WHILE the caller's workspace is still open (before its
        # `async with workspace()` exits and rmtree's the temp dir) — capture
        # the on-disk reality here, not after run_screenwriter() returns.
        user_parts = kwargs["user_parts"]
        image_parts = [p for p in user_parts if p.get("type") == "image_file"]
        captured["image_count"] = len(image_parts)
        captured["all_paths_exist_with_content"] = all(
            _Path(p["data"]).is_absolute()
            and _Path(p["data"]).exists()
            and len(_Path(p["data"]).read_bytes()) > 0
            for p in image_parts
        )
        return FAKE_STORYBOARD

    fake_provider = MagicMock()
    fake_provider.generate_json = fake_generate_json
    monkeypatch.setattr(tasks, "get_provider", lambda: fake_provider)

    ctx = {"session_factory": db_session_factory, "redis": redis}
    await tasks.run_screenwriter(ctx, PID, "user:tester")

    assert "image_count" in captured, "screenwriter LLM call (generate_json) was never made"
    assert captured["image_count"] == N_REF_IMAGES, (
        f"Expected {N_REF_IMAGES} reference image(s) to reach the LLM call, got "
        f"{captured['image_count']}. reference_images_data was built from COS keys "
        "without fetching them into a local workspace first — Path(key).exists() is "
        "always False for a COS key, so images are silently dropped (C2)."
    )
    # And they must be REAL local files with content, not the raw COS key strings
    # (a bare key string would also make the count assertion above pass under a
    # half-fix that stops filtering but still hands the key through unfetched;
    # llm.py's _build_contents does its own Path(p).exists() check downstream,
    # which would then silently drop them a second time).
    assert captured["all_paths_exist_with_content"], (
        "reference image paths reaching the LLM call must be real, fetched local "
        "files — not raw COS keys (which llm.py's Path(p).exists() would drop again)."
    )
