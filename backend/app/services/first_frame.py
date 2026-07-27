"""First-frame resolution — the single source of truth for a shot's first frame.

There is exactly ONE stored first-frame field: ``Shot.custom_first_frame_path``
(the 首帧 slot, set by upload / extract / use-prev-last-frame / set-as-base). The
frame actually fed to the video model is a pure function of that field plus
continuity, computed on demand by :func:`pick_first_frame`. Nothing persists the
resolved frame — that avoids the staleness class of bug where a cached
"resolved" path diverges from the user's re-uploaded 首帧.

Both the worker (at generation time) and the API (the 提取本镜首帧 endpoint) call
:func:`pick_first_frame`, so resolution can never drift between them.
"""
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Shot, ReferenceImage
from app.services import object_store


async def pick_first_frame(
    project_id: str, shot: Shot, session: AsyncSession
) -> Optional[Path]:
    """
    Resolve the first frame for a shot.

    The 首帧 slot (custom_first_frame_path) is AUTHORITATIVE when set — it anchors
    the character's identity for the whole clip, so it wins over multi-image mode.
    Only when there is no explicit first frame does a shot with custom_reference_paths
    fall into multi-image mode (return None → caller uses reference_images).
    For shot 1 without custom images: fall back to project character reference.
    For connected shots without a first frame: use previous shot's last frame.

    All *_path fields hold COS keys (not local filesystem paths) — "existence"
    is decided against the object store, not the local disk.
    """
    # Single custom first frame (the 首帧 slot) — authoritative; anchors identity.
    if shot.custom_first_frame_path:
        if await object_store.exists(shot.custom_first_frame_path):
            return Path(shot.custom_first_frame_path)

    # Multi-image reference mode → return None (caller uses reference_images).
    # Only when there is NO explicit first frame above.
    if shot.custom_reference_paths and not shot.align_with_previous:
        return None

    # Try to get previous shot's last frame (for both connected and disconnected shots)
    if shot.shot_id > 1:
        prev_result = await session.execute(
            select(Shot).where(
                Shot.project_id == project_id, Shot.shot_id == shot.shot_id - 1
            )
        )
        prev_shot = prev_result.scalar_one_or_none()
        if prev_shot and prev_shot.last_frame_path:
            if await object_store.exists(prev_shot.last_frame_path):
                return Path(prev_shot.last_frame_path)

    # Fallback to character reference
    return await get_first_character_ref(project_id, session)


async def get_first_character_ref(project_id: str, session: AsyncSession) -> Path:
    """Get first character reference image for a project."""
    result = await session.execute(
        select(ReferenceImage)
        .where(
            ReferenceImage.project_id == project_id, ReferenceImage.kind == "character"
        )
        .order_by(ReferenceImage.order_index)
        .limit(1)
    )
    ref = result.scalar_one_or_none()

    if not ref:
        raise ValueError("No character reference image found")

    if not await object_store.exists(ref.storage_path):
        raise ValueError(f"Reference image not found: {ref.storage_path}")

    return Path(ref.storage_path)


async def init_shot1_first_frame(
    project_id: str, shot: Shot, session: AsyncSession
) -> None:
    """Eagerly populate shot 1's custom_first_frame_path from the first character ref.

    Write-only-when-empty: if the field is already set (e.g. via upload), leave it.
    Non-raising: if no character ref exists or the field is already set, do nothing.
    This is for frontend visibility only — pick_first_frame remains authoritative at
    gen-time.
    """
    if shot.shot_id != 1 or shot.custom_first_frame_path:
        return

    result = await session.execute(
        select(ReferenceImage)
        .where(
            ReferenceImage.project_id == project_id,
            ReferenceImage.kind == "character",
        )
        .order_by(ReferenceImage.order_index)
        .limit(1)
    )
    ref = result.scalar_one_or_none()
    if ref:
        shot.custom_first_frame_path = ref.storage_path
        session.add(shot)


async def propagate_first_frame_to_next(
    project_id: str, shot: Shot, last_frame_path: str, session: AsyncSession
) -> None:
    """Eagerly point the NEXT shot's custom_first_frame_path at this shot's last frame.

    Predicate: next shot (shot_id + 1) must exist AND use_prev_last_frame=True.

    Last frames now use unique filenames (last_frame_<ts>_<hex>.png), so the value
    must be RE-POINTED on every (re)generation/trim — a previously auto-propagated
    path would otherwise reference a deleted, stale file. We preserve a genuine user
    override (a frame uploaded/extracted into custom_frames/) and only re-point when
    the existing value is empty or itself an auto-propagated last frame.
    """
    result = await session.execute(
        select(Shot).where(
            Shot.project_id == project_id,
            Shot.shot_id == shot.shot_id + 1,
        )
    )
    next_shot = result.scalar_one_or_none()
    if next_shot is None or not next_shot.use_prev_last_frame:
        return
    # Only auto-adjust the NEXT shot while it is still un-generated — once it has
    # its own rendered video, leave its first frame alone (changing it would
    # mismatch the already-generated clip).
    if next_shot.video_path:
        return
    existing = next_shot.custom_first_frame_path
    is_user_override = bool(existing) and "custom_frames" in existing
    if not is_user_override and existing != last_frame_path:
        next_shot.custom_first_frame_path = last_frame_path
        session.add(next_shot)
