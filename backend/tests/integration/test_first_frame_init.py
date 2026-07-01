"""Integration tests for Task 8: explicit first-frame continuity initialization.

TDD: write tests first (RED), then implement the helpers (GREEN).

Tests:
1. Shot-1 init: project with a character ref + shot1 (custom_first_frame_path=None)
   → after helper, shot1.custom_first_frame_path == that ref's storage_path.
2. Shot-1 init, NO character ref → shot1.custom_first_frame_path stays None, no exception raised.
3. Continuity: shot N (last_frame_path set) + shot N+1 (use_prev_last_frame=True,
   custom_first_frame_path=None) → after helper, N+1.custom_first_frame_path == N's last_frame_path.
4. Continuity preserves override: N+1.custom_first_frame_path already set → unchanged.
5. Continuity respects flag: N+1.use_prev_last_frame=False → custom_first_frame_path stays None.
"""
import pytest
from sqlalchemy import select

from app.models.project import Project, Shot, ReferenceImage, ProjectStatus, ShotStatus
import worker.tasks as tasks


# ── helpers ───────────────────────────────────────────────────────────────────

async def _seed_project(sf, project_id: str) -> None:
    async with sf() as s:
        s.add(Project(
            id=project_id,
            title="Test",
            theme_text="theme",
            creator_name="tester",
            status=ProjectStatus.DRAFT.value,
            aspect_ratio="9:16",
        ))
        await s.commit()


async def _seed_shot(sf, project_id: str, shot_id: int, **kwargs) -> None:
    async with sf() as s:
        s.add(Shot(
            project_id=project_id,
            shot_id=shot_id,
            text=f"Shot {shot_id}",
            shot_type="Medium Shot",
            visual_description=f"Visual {shot_id}",
            shot_duration=6,
            status=ShotStatus.PENDING.value,
            align_with_previous=(shot_id > 1),
            **kwargs,
        ))
        await s.commit()


async def _seed_char_ref(sf, project_id: str, storage_path: str) -> None:
    async with sf() as s:
        s.add(ReferenceImage(
            project_id=project_id,
            kind="character",
            filename="char.jpg",
            storage_path=storage_path,
            order_index=0,
        ))
        await s.commit()


async def _get_shot(sf, project_id: str, shot_id: int) -> Shot:
    async with sf() as s:
        result = await s.execute(
            select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
        )
        return result.scalar_one()


# ── tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_shot1_init_sets_char_ref_path(db_session_factory):
    """Shot-1 init: project with a character ref → custom_first_frame_path is set."""
    pid = "proj-ff-1"
    char_path = f"/fake/{pid}/char.jpg"

    await _seed_project(db_session_factory, pid)
    await _seed_shot(db_session_factory, pid, shot_id=1, custom_first_frame_path=None)
    await _seed_char_ref(db_session_factory, pid, storage_path=char_path)

    async with db_session_factory() as session:
        shot = (await session.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        await tasks._init_shot1_first_frame(pid, shot, session)
        await session.commit()

    shot_after = await _get_shot(db_session_factory, pid, shot_id=1)
    assert shot_after.custom_first_frame_path == char_path


@pytest.mark.asyncio
async def test_shot1_init_no_char_ref_stays_none(db_session_factory):
    """Shot-1 init with NO character ref → custom_first_frame_path stays None, no exception."""
    pid = "proj-ff-2"

    await _seed_project(db_session_factory, pid)
    await _seed_shot(db_session_factory, pid, shot_id=1, custom_first_frame_path=None)
    # No character ref added

    async with db_session_factory() as session:
        shot = (await session.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        # Must NOT raise even when there is no character ref
        await tasks._init_shot1_first_frame(pid, shot, session)
        await session.commit()

    shot_after = await _get_shot(db_session_factory, pid, shot_id=1)
    assert shot_after.custom_first_frame_path is None


@pytest.mark.asyncio
async def test_continuity_propagates_last_frame_to_next(db_session_factory):
    """After shot N generates, next shot's custom_first_frame_path is filled."""
    pid = "proj-ff-3"
    last_frame = f"/fake/{pid}/shots/1/last_frame.png"

    await _seed_project(db_session_factory, pid)
    await _seed_shot(db_session_factory, pid, shot_id=1, last_frame_path=last_frame)
    await _seed_shot(
        db_session_factory, pid, shot_id=2,
        use_prev_last_frame=True,
        custom_first_frame_path=None,
    )

    async with db_session_factory() as session:
        shot1 = (await session.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        await tasks._propagate_first_frame_to_next(pid, shot1, last_frame, session)
        await session.commit()

    shot2_after = await _get_shot(db_session_factory, pid, shot_id=2)
    assert shot2_after.custom_first_frame_path == last_frame


@pytest.mark.asyncio
async def test_continuity_preserves_user_override(db_session_factory):
    """Continuity helper must NOT overwrite an existing custom_first_frame_path."""
    pid = "proj-ff-4"
    last_frame = f"/fake/{pid}/shots/1/last_frame.png"
    user_override = f"/fake/{pid}/shots/2/custom_frames/1234567890_abcd1234.png"

    await _seed_project(db_session_factory, pid)
    await _seed_shot(db_session_factory, pid, shot_id=1, last_frame_path=last_frame)
    await _seed_shot(
        db_session_factory, pid, shot_id=2,
        use_prev_last_frame=True,
        custom_first_frame_path=user_override,  # already set by user
    )

    async with db_session_factory() as session:
        shot1 = (await session.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        await tasks._propagate_first_frame_to_next(pid, shot1, last_frame, session)
        await session.commit()

    shot2_after = await _get_shot(db_session_factory, pid, shot_id=2)
    # Override must be preserved
    assert shot2_after.custom_first_frame_path == user_override


@pytest.mark.asyncio
async def test_continuity_repoints_stale_auto_frame(db_session_factory):
    """A previously auto-propagated last frame must be RE-POINTED to the new last
    frame — with unique filenames the old path references a deleted/stale file."""
    pid = "proj-ff-6"
    old_lf = f"/fake/{pid}/shots/1/last_frame_111_aaaaaaaa.png"
    new_lf = f"/fake/{pid}/shots/1/last_frame_222_bbbbbbbb.png"

    await _seed_project(db_session_factory, pid)
    await _seed_shot(db_session_factory, pid, shot_id=1, last_frame_path=new_lf)
    await _seed_shot(
        db_session_factory, pid, shot_id=2,
        use_prev_last_frame=True,
        custom_first_frame_path=old_lf,  # auto-propagated by an earlier generation
    )

    async with db_session_factory() as session:
        shot1 = (await session.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        await tasks._propagate_first_frame_to_next(pid, shot1, new_lf, session)
        await session.commit()

    shot2_after = await _get_shot(db_session_factory, pid, shot_id=2)
    assert shot2_after.custom_first_frame_path == new_lf


@pytest.mark.asyncio
async def test_continuity_respects_use_prev_last_frame_false(db_session_factory):
    """Continuity helper must NOT set custom_first_frame_path when flag is False."""
    pid = "proj-ff-5"
    last_frame = f"/fake/{pid}/shots/1/last_frame.png"

    await _seed_project(db_session_factory, pid)
    await _seed_shot(db_session_factory, pid, shot_id=1, last_frame_path=last_frame)
    await _seed_shot(
        db_session_factory, pid, shot_id=2,
        use_prev_last_frame=False,   # disconnected shot
        custom_first_frame_path=None,
    )

    async with db_session_factory() as session:
        shot1 = (await session.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        await tasks._propagate_first_frame_to_next(pid, shot1, last_frame, session)
        await session.commit()

    shot2_after = await _get_shot(db_session_factory, pid, shot_id=2)
    assert shot2_after.custom_first_frame_path is None


@pytest.mark.asyncio
async def test_continuity_skips_already_generated_next(db_session_factory):
    """If the next shot is already generated (has video_path), its first frame must
    NOT be auto-adjusted — only un-generated shots track the previous last frame."""
    pid = "proj-ff-7"
    new_lf = f"/fake/{pid}/shots/1/last_frame_999_cccccccc.png"
    stale = f"/fake/{pid}/shots/1/last_frame_111_aaaaaaaa.png"

    await _seed_project(db_session_factory, pid)
    await _seed_shot(db_session_factory, pid, shot_id=1, last_frame_path=new_lf)
    await _seed_shot(
        db_session_factory, pid, shot_id=2,
        use_prev_last_frame=True,
        custom_first_frame_path=stale,
        video_path=f"/fake/{pid}/shots/2/output_1_x.mp4",  # already generated
    )

    async with db_session_factory() as session:
        shot1 = (await session.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        await tasks._propagate_first_frame_to_next(pid, shot1, new_lf, session)
        await session.commit()

    shot2_after = await _get_shot(db_session_factory, pid, shot_id=2)
    # Already-generated next shot is left untouched (NOT repointed to new_lf).
    assert shot2_after.custom_first_frame_path == stale
