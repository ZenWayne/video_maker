"""pick_first_frame / propagate_first_frame_to_next against REAL COS keys.

test_custom_first_frame_priority.py proves the resolution LOGIC with a stubbed
object_store.exists() oracle (local files). This file proves the same chain
against a REAL bucket: shot 1's last_frame is a real COS object; shot 2 (no
custom_first_frame_path of its own) must resolve it via pick_first_frame's
object_store.exists() check — not fall back to the project's character
reference. This is the exact silent-breakage scenario the review found:
propagate_first_frame_to_next now writes a real COS key into
next_shot.custom_first_frame_path, and pick_first_frame must actually
recognize it as existing.
"""
from sqlalchemy import select

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import _make_project, _add_shot, _add_character_image

from app.models.project import Shot
from app.services import object_store
from app.services.storage import shot_key
from app.services.first_frame import pick_first_frame, propagate_first_frame_to_next

pytestmark = requires_cos


async def test_pick_first_frame_resolves_prev_shot_real_cos_last_frame(
    db_session_factory, cos_prefix, tmp_path
):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="completed")
    await _add_shot(db_session_factory, pid, 2, status="pending")
    # A character ref exists too — if the fix regresses, pick_first_frame would
    # silently fall back to this instead of shot 1's real last frame.
    await _add_character_image(db_session_factory, pid)

    last_frame_key = shot_key(pid, 1, "last_frame_realcos.png")
    f = tmp_path / "lf.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 32)
    await object_store.put(last_frame_key, f)

    async with db_session_factory() as s:
        shot1 = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot1.last_frame_path = last_frame_key
        await s.commit()

    async with db_session_factory() as s:
        shot2 = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 2)
        )).scalar_one()
        resolved = await pick_first_frame(pid, shot2, s)

    assert str(resolved) == last_frame_key, (
        f"pick_first_frame returned {resolved!r} instead of shot 1's real COS "
        f"last_frame {last_frame_key!r} — it silently fell back to the "
        "character reference image, exactly the bug this task fixes."
    )


async def test_propagate_then_pick_first_frame_chain_uses_real_cos_key(
    db_session_factory, cos_prefix, tmp_path
):
    """End-to-end: propagate_first_frame_to_next writes a real COS key into the
    next shot, and pick_first_frame on that next shot resolves it back out."""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="completed")
    await _add_shot(db_session_factory, pid, 2, status="pending")

    last_frame_key = shot_key(pid, 1, "last_frame_propagated.png")
    f = tmp_path / "lf2.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"2" * 32)
    await object_store.put(last_frame_key, f)

    async with db_session_factory() as s:
        shot1 = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot2 = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 2)
        )).scalar_one()
        assert shot2.use_prev_last_frame is True
        await propagate_first_frame_to_next(pid, shot1, last_frame_key, s)
        await s.commit()

    async with db_session_factory() as s:
        shot2 = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 2)
        )).scalar_one()
        assert shot2.custom_first_frame_path == last_frame_key
        resolved = await pick_first_frame(pid, shot2, s)

    assert str(resolved) == last_frame_key
