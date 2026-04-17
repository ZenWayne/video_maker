"""Pipeline API routes for video generation workflow."""

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Header, UploadFile, File
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.main import get_redis
from app.models.project import Project, Shot, ReferenceImage
from app.models.schemas import (
    ProjectResponse, StoryboardUpdate, ShotUpdate, ShotAiEditRequest,
    ShotTrimRequest, RegenerateShotsRequest, PipelineActionResponse,
    ReferenceVoiceRequest,
)
from app.services.state_machine import (
    ProjectStatus, ShotStatus,
    transition_project_status, InvalidTransitionError
)
from app.services.storage import (
    storyboard_path, archived_storyboard_path, shot_custom_frames_dir, to_media_url,
    shot_pre_vc_video_path, shot_audio_original_path, shot_audio_vc_path,
)
from app.services.events import publish_event

router = APIRouter()


def _require_user(x_user_name: Optional[str] = Header(default=None)) -> str:
    """Require X-User-Name header."""
    if not x_user_name:
        raise HTTPException(status_code=400, detail="X-User-Name header required")
    return x_user_name


async def _get_project_or_404(project_id: str, session: AsyncSession) -> Project:
    """Get project or raise 404."""
    result = await session.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def _get_arq_redis(redis) -> ArqRedis:
    """Get ArqRedis from redis client."""
    return ArqRedis(redis.connection_pool)


@router.post("/projects/{project_id}/start", status_code=202)
async def start_project(
    project_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """Start the video generation pipeline (transition to SCRIPTING)."""
    project = await _get_project_or_404(project_id, session)

    # Validate at least one character image
    result = await session.execute(
        select(ReferenceImage).where(
            ReferenceImage.project_id == project_id,
            ReferenceImage.kind == "character",
        )
    )
    if not result.scalars().first():
        raise HTTPException(
            status_code=400,
            detail="At least one character reference image required"
        )

    # Transition status
    try:
        await transition_project_status(
            project, ProjectStatus.SCRIPTING, f"user:{user}", session, redis
        )
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    # Enqueue screenwriter task
    arq = await _get_arq_redis(redis)
    await arq.enqueue_job("run_screenwriter", project_id, f"user:{user}")

    return {"status": "queued", "message": "Screenwriter task queued"}


@router.post("/projects/{project_id}/regenerate-script", status_code=202)
async def regenerate_script(
    project_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """Regenerate script (archive current, clear shots, restart)."""
    project = await _get_project_or_404(project_id, session)

    # Archive current storyboard
    sb_path = storyboard_path(project_id)
    if sb_path.exists():
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        sb_path.rename(archived_storyboard_path(project_id, ts))

    # Clear shots
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id)
    )
    for shot in result.scalars().all():
        await session.delete(shot)

    # Transition to SCRIPTING
    try:
        await transition_project_status(
            project, ProjectStatus.SCRIPTING, f"user:{user}", session, redis
        )
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    # Enqueue screenwriter task
    arq = await _get_arq_redis(redis)
    await arq.enqueue_job("run_screenwriter", project_id, f"user:{user}")

    return {"status": "queued", "message": "Script regeneration queued"}


@router.patch("/projects/{project_id}/storyboard", response_model=ProjectResponse)
async def patch_storyboard(
    project_id: str,
    body: StoryboardUpdate,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Update storyboard (scene_overview and/or shots)."""
    project = await _get_project_or_404(project_id, session)

    if project.status != ProjectStatus.SCRIPT_REVIEW.value:
        raise HTTPException(
            status_code=409,
            detail="Project must be in script_review status to edit storyboard"
        )

    if body.scene_overview is not None:
        project.scene_overview = body.scene_overview

    if body.shots is not None:
        # Update shots
        result = await session.execute(
            select(Shot).where(Shot.project_id == project_id)
        )
        shots_by_id = {s.shot_id: s for s in result.scalars().all()}

        for item in body.shots:
            shot = shots_by_id.get(item.shot_id)
            if shot:
                shot.text = item.text
                shot.shot_type = item.shot_type
                shot.visual_description = item.visual_description
                shot.shot_duration = item.shot_duration
                shot.align_with_previous = item.align_with_previous
                session.add(shot)

    project.updated_at = datetime.utcnow()
    session.add(project)
    await session.commit()
    await session.refresh(project)

    # Reload storyboard
    from app.models.schemas import Storyboard
    storyboard = None
    if project.storyboard_path:
        try:
            sb_data = json.loads(Path(project.storyboard_path).read_text())
            storyboard = Storyboard(**sb_data)
        except Exception:
            pass

    return ProjectResponse(
        id=project.id,
        title=project.title,
        theme_text=project.theme_text,
        creator_name=project.creator_name,
        status=project.status,
        scene_overview=project.scene_overview,
        storyboard_path=project.storyboard_path,
        final_video_path=project.final_video_path,
        error_message=project.error_message,
        created_at=project.created_at,
        updated_at=project.updated_at,
        reference_images=[],
        shots=[],
        storyboard=storyboard,
    )


@router.post("/projects/{project_id}/approve-script", status_code=202)
async def approve_script(
    project_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """Approve script and start shot generation."""
    project = await _get_project_or_404(project_id, session)

    try:
        await transition_project_status(
            project, ProjectStatus.SHOT_GENERATING, f"user:{user}", session, redis
        )
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    # Reset all shots to PENDING
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id)
    )
    for shot in result.scalars().all():
        shot.status = ShotStatus.PENDING.value
        shot.error_message = None
        session.add(shot)
    await session.commit()

    # Enqueue shot pipeline task
    arq = await _get_arq_redis(redis)
    await arq.enqueue_job("run_shot_pipeline", project_id, f"user:{user}")

    return {"status": "queued", "message": "Shot generation queued"}


@router.post("/projects/{project_id}/regenerate-shots", status_code=202)
async def regenerate_shots(
    project_id: str,
    body: RegenerateShotsRequest,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """Regenerate specific shots."""
    project = await _get_project_or_404(project_id, session)

    try:
        await transition_project_status(
            project, ProjectStatus.SHOT_GENERATING, f"user:{user}", session, redis
        )
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    # Reset specified shots to PENDING
    result = await session.execute(
        select(Shot).where(
            Shot.project_id == project_id,
            Shot.shot_id.in_(body.shot_ids),
        )
    )
    for shot in result.scalars().all():
        shot.status = ShotStatus.PENDING.value
        shot.error_message = None
        session.add(shot)
    await session.commit()

    # Enqueue shot pipeline task
    arq = await _get_arq_redis(redis)
    await arq.enqueue_job("run_shot_pipeline", project_id, f"user:{user}")

    return {"status": "queued", "message": "Shot regeneration queued"}


@router.post("/projects/{project_id}/continue-generation", status_code=202)
async def continue_generation(
    project_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """Continue generating the next pending shot (approve current, generate next)."""
    project = await _get_project_or_404(project_id, session)

    if project.status != ProjectStatus.SHOT_REVIEW.value:
        raise HTTPException(status_code=409, detail="Project must be in shot_review status")

    # Check at least one pending shot exists
    result = await session.execute(
        select(Shot).where(
            Shot.project_id == project_id,
            Shot.status.in_([ShotStatus.PENDING.value, ShotStatus.FAILED.value]),
        )
    )
    if not result.scalars().first():
        raise HTTPException(status_code=400, detail="No pending shots to generate")

    try:
        await transition_project_status(
            project, ProjectStatus.SHOT_GENERATING, f"user:{user}", session, redis
        )
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    arq = await _get_arq_redis(redis)
    await arq.enqueue_job("run_shot_pipeline", project_id, f"user:{user}")

    return {"status": "queued", "message": "Next shot generation queued"}


@router.patch("/projects/{project_id}/shots/{shot_id}")
async def patch_shot(
    project_id: str,
    shot_id: int,
    body: ShotUpdate,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Update shot (motion_prompt or align_with_previous)."""
    result = await session.execute(
        select(Shot).where(
            Shot.project_id == project_id,
            Shot.shot_id == shot_id,
        )
    )
    shot = result.scalar_one_or_none()
    if shot is None:
        raise HTTPException(status_code=404, detail="Shot not found")

    if body.motion_prompt is not None:
        shot.motion_prompt = body.motion_prompt
    if body.text is not None:
        shot.text = body.text
    if body.visual_description is not None:
        shot.visual_description = body.visual_description
    if body.align_with_previous is not None:
        shot.align_with_previous = body.align_with_previous
    if body.use_prev_last_frame is not None:
        shot.use_prev_last_frame = body.use_prev_last_frame
    if body.shot_duration is not None:
        shot.shot_duration = body.shot_duration

    shot.updated_at = datetime.utcnow()
    session.add(shot)
    await session.commit()
    await session.refresh(shot)

    return {
        "shot_id": shot.shot_id,
        "text": shot.text,
        "visual_description": shot.visual_description,
        "motion_prompt": shot.motion_prompt,
        "align_with_previous": shot.align_with_previous,
        "use_prev_last_frame": shot.use_prev_last_frame,
        "shot_duration": shot.shot_duration,
    }


@router.post("/projects/{project_id}/shots/{shot_id}/ai-edit")
async def ai_edit_shot(
    project_id: str,
    shot_id: int,
    body: ShotAiEditRequest,
):
    """Use AI to revise a shot based on a user instruction."""
    from app.agents.shot_editor import run_shot_editor
    from app.db import AsyncSession as session_factory

    # Fetch all needed data, then release the session before calling the LLM.
    # Keeping a session open during a long LLM call exhausts the DB connection pool.
    async with session_factory() as session:
        result = await session.execute(
            select(Project).where(Project.id == project_id)
        )
        project = result.scalar_one_or_none()
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        shots_result = await session.execute(
            select(Shot).where(Shot.project_id == project_id).order_by(Shot.shot_id)
        )
        all_shots = shots_result.scalars().all()

        shot = next((s for s in all_shots if s.shot_id == shot_id), None)
        if shot is None:
            raise HTTPException(status_code=404, detail="Shot not found")

        shot_list = list(all_shots)
        idx = shot_list.index(shot)

        def _ctx(s):
            return {"text": s.text, "visual_description": s.visual_description} if s else None

        editor_kwargs = dict(
            instruction=body.instruction,
            current_text=shot.text,
            current_visual=shot.visual_description or "",
            shot_type=shot.shot_type,
            shot_duration=shot.shot_duration,
            theme_text=project.theme_text or "",
            scene_overview=project.scene_overview or "",
            prev_shot=_ctx(shot_list[idx - 1] if idx > 0 else None),
            next_shot=_ctx(shot_list[idx + 1] if idx < len(shot_list) - 1 else None),
            align_with_previous=shot.align_with_previous,
            shot_id=shot.shot_id,
            has_reference_images=bool(shot.custom_reference_paths),
        )
    # Session released here — now safe to do the long LLM call
    # Provider is selected inside run_shot_editor (DeepSeek if key set, else Gemini)
    try:
        result = await run_shot_editor(**editor_kwargs)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI generation failed: {e}")

    return result


@router.post("/projects/{project_id}/export", status_code=202)
async def export_project(
    project_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """Export final video by merging all completed shots."""
    project = await _get_project_or_404(project_id, session)

    # Check all shots are completed
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id)
    )
    shots = result.scalars().all()

    if any(s.status != ShotStatus.COMPLETED.value for s in shots):
        raise HTTPException(
            status_code=400,
            detail="All shots must be COMPLETED before export"
        )

    try:
        await transition_project_status(
            project, ProjectStatus.EXPORTING, f"user:{user}", session, redis
        )
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    # Enqueue merger task
    arq = await _get_arq_redis(redis)
    await arq.enqueue_job("run_merger", project_id, f"user:{user}")

    return {"status": "queued", "message": "Export queued"}


@router.post("/projects/{project_id}/cancel-generation", status_code=202)
async def cancel_generation(
    project_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """Cancel shot generation and return to shot review."""
    project = await _get_project_or_404(project_id, session)

    try:
        await transition_project_status(
            project, ProjectStatus.SHOT_REVIEW, f"user:{user}", session, redis
        )
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"status": "shot_review", "message": "Generation cancelled"}


@router.post("/projects/{project_id}/reset-to-script", status_code=202)
async def reset_to_script(
    project_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """Return project to script review without regenerating (preserves storyboard and shots)."""
    project = await _get_project_or_404(project_id, session)

    try:
        await transition_project_status(
            project, ProjectStatus.SCRIPT_REVIEW, f"user:{user}", session, redis
        )
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"status": "script_review", "message": "Returned to script review"}


@router.post("/projects/{project_id}/reset")
async def reset_project(
    project_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """Reset project to DRAFT status."""
    project = await _get_project_or_404(project_id, session)

    # Archive storyboard
    sb_path = storyboard_path(project_id)
    if sb_path.exists():
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        sb_path.rename(archived_storyboard_path(project_id, ts))

    # Clear shots
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id)
    )
    for shot in result.scalars().all():
        await session.delete(shot)

    # Clear error message
    project.error_message = None

    try:
        await transition_project_status(
            project, ProjectStatus.DRAFT, f"user:{user}", session, redis
        )
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"status": "draft", "message": "Project reset to draft"}


@router.post("/projects/{project_id}/shots/{shot_id}/reference-images")
async def upload_shot_references(
    project_id: str,
    shot_id: int,
    files: list[UploadFile] = File(...),
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Upload custom reference images for a disconnected shot.

    Single image → replaces first frame (image-to-video mode).
    Multiple images → used as reference_images (ASSET mode).
    """
    import uuid as _uuid

    project = await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    # Create storage directory
    dest_dir = shot_custom_frames_dir(project_id, shot_id)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Collect existing paths
    existing_paths: list[str] = []
    if shot.custom_reference_paths:
        existing_paths = json.loads(shot.custom_reference_paths)
    elif shot.custom_first_frame_path:
        existing_paths = [shot.custom_first_frame_path]

    # Save new files (append)
    for upload in files:
        content = await upload.read()
        safe_name = Path(upload.filename).name if upload.filename else "image.png"
        image_id = str(_uuid.uuid4())[:8]
        dest_path = dest_dir / f"{image_id}_{safe_name}"
        dest_path.write_bytes(content)
        existing_paths.append(str(dest_path))

    # Update DB: 1 image → first frame mode, multiple → reference_images mode
    all_paths = existing_paths
    if len(all_paths) == 1:
        shot.custom_first_frame_path = all_paths[0]
        shot.custom_reference_paths = None
    else:
        shot.custom_first_frame_path = None
        shot.custom_reference_paths = json.dumps(all_paths)

    await session.commit()
    return _ref_images_response(shot)


@router.delete("/projects/{project_id}/shots/{shot_id}/reference-images")
async def delete_shot_references(
    project_id: str,
    shot_id: int,
    index: Optional[int] = None,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Delete custom reference images for a shot.

    If index is provided, delete only that image. Otherwise delete all.
    """
    project = await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    if index is not None:
        # Delete single image by index
        all_paths: list[str] = []
        if shot.custom_reference_paths:
            all_paths = json.loads(shot.custom_reference_paths)
        elif shot.custom_first_frame_path:
            all_paths = [shot.custom_first_frame_path]

        if index < 0 or index >= len(all_paths):
            raise HTTPException(status_code=400, detail="Invalid index")

        # Delete file
        removed = Path(all_paths.pop(index))
        removed.unlink(missing_ok=True)

        # Update DB
        if len(all_paths) == 0:
            shot.custom_first_frame_path = None
            shot.custom_reference_paths = None
        elif len(all_paths) == 1:
            shot.custom_first_frame_path = all_paths[0]
            shot.custom_reference_paths = None
        else:
            shot.custom_first_frame_path = None
            shot.custom_reference_paths = json.dumps(all_paths)
    else:
        # Delete all
        dest_dir = shot_custom_frames_dir(project_id, shot_id)
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shot.custom_first_frame_path = None
        shot.custom_reference_paths = None

    await session.commit()

    return _ref_images_response(shot)


def _ref_images_response(shot: Shot) -> dict:
    return {
        "shot_id": shot.shot_id,
        "custom_first_frame_path": to_media_url(shot.custom_first_frame_path),
        "custom_reference_paths": (
            [to_media_url(p) for p in json.loads(shot.custom_reference_paths)]
            if shot.custom_reference_paths else None
        ),
    }


@router.put("/projects/{project_id}/shots/{shot_id}/reference-images/reorder")
async def reorder_shot_references(
    project_id: str,
    shot_id: int,
    body: dict,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Reorder reference images by providing new index order."""
    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    order = body.get("order", [])

    all_paths = []
    if shot.custom_reference_paths:
        all_paths = json.loads(shot.custom_reference_paths)
    elif shot.custom_first_frame_path:
        all_paths = [shot.custom_first_frame_path]

    if len(order) != len(all_paths):
        raise HTTPException(status_code=400, detail="Order length mismatch")

    reordered = [all_paths[i] for i in order]

    if len(reordered) == 1:
        shot.custom_first_frame_path = reordered[0]
        shot.custom_reference_paths = None
    else:
        shot.custom_first_frame_path = None
        shot.custom_reference_paths = json.dumps(reordered)

    await session.commit()
    return _ref_images_response(shot)


@router.get("/projects/{project_id}/shots/{shot_id}/video-info")
async def get_shot_video_info(
    project_id: str,
    shot_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Return video metadata (fps, total_frames, duration) via ffprobe."""
    from app.agents.video_trimmer import get_video_info

    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot or not shot.video_path:
        raise HTTPException(status_code=404, detail="Shot or video not found")

    return get_video_info(shot.video_path)


@router.post("/projects/{project_id}/shots/{shot_id}/trim")
async def trim_shot_video(
    project_id: str,
    shot_id: int,
    body: ShotTrimRequest,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Trim video to end at the given frame number (inclusive)."""
    from app.agents.video_trimmer import get_video_info, trim_video
    from app.agents.frame_porter import extract_last_frame

    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot or not shot.video_path:
        raise HTTPException(status_code=404, detail="Shot or video not found")
    if shot.status != "completed":
        raise HTTPException(status_code=409, detail="Shot is not completed")

    video_path = Path(shot.video_path)
    info = get_video_info(str(video_path))

    if body.end_frame < 24:
        raise HTTPException(status_code=400, detail="Must keep at least 24 frames")
    if body.end_frame >= info["total_frames"]:
        raise HTTPException(status_code=400, detail="end_frame must be less than total frames")

    end_time = body.end_frame / info["fps"]

    # Backup original on first trim
    backup = video_path.with_name("output_original.mp4")
    if not backup.exists():
        video_path.rename(backup)
    source = str(backup)

    # Trim
    trim_video(source, str(video_path), end_time)

    # Re-extract last frame
    if shot.last_frame_path:
        extract_last_frame(str(video_path), shot.last_frame_path)

    ts = int(datetime.utcnow().timestamp())
    await session.commit()

    return {
        "video_path": to_media_url(str(video_path)),
        "last_frame_path": to_media_url(shot.last_frame_path),
        "version": ts,
        **get_video_info(str(video_path)),
    }


# ============== Voice Cloning ==============


@router.post("/projects/{project_id}/reference-voice")
async def set_reference_voice(
    project_id: str,
    body: ReferenceVoiceRequest,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Set the reference voice shot for voice cloning."""
    project = await _get_project_or_404(project_id, session)

    # Validate shot exists and is completed
    result = await session.execute(
        select(Shot).where(
            Shot.project_id == project_id, Shot.shot_id == body.shot_id
        )
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")
    if shot.status != ShotStatus.COMPLETED.value:
        raise HTTPException(status_code=400, detail="Shot must be completed")
    if not shot.video_path:
        raise HTTPException(status_code=400, detail="Shot has no video")

    project.reference_voice_shot_id = body.shot_id
    project.updated_at = datetime.utcnow()
    session.add(project)
    await session.commit()

    return {"reference_voice_shot_id": body.shot_id}


@router.delete("/projects/{project_id}/reference-voice")
async def clear_reference_voice(
    project_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Clear the reference voice setting."""
    project = await _get_project_or_404(project_id, session)

    project.reference_voice_shot_id = None
    project.updated_at = datetime.utcnow()
    session.add(project)
    await session.commit()

    return {"reference_voice_shot_id": None}


@router.post("/projects/{project_id}/shots/{shot_id}/voice-convert", status_code=202)
async def voice_convert_shot(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """Convert a shot's voice to match the reference voice."""
    project = await _get_project_or_404(project_id, session)

    if not project.reference_voice_shot_id:
        raise HTTPException(status_code=400, detail="No reference voice set")

    if shot_id == project.reference_voice_shot_id:
        raise HTTPException(status_code=400, detail="Cannot convert the reference shot itself")

    result = await session.execute(
        select(Shot).where(
            Shot.project_id == project_id, Shot.shot_id == shot_id
        )
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")
    if shot.status != ShotStatus.COMPLETED.value:
        raise HTTPException(status_code=400, detail="Shot must be completed")

    shot.vc_status = "converting"
    shot.vc_error_message = None
    session.add(shot)
    await session.commit()

    arq = await _get_arq_redis(redis)
    await arq.enqueue_job(
        "run_voice_convert", project_id, shot_id, f"user:{user}"
    )

    return {"status": "queued", "shot_id": shot_id}


@router.post("/projects/{project_id}/voice-convert-all", status_code=202)
async def voice_convert_all(
    project_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """Convert all non-reference completed shots to match the reference voice."""
    project = await _get_project_or_404(project_id, session)

    if not project.reference_voice_shot_id:
        raise HTTPException(status_code=400, detail="No reference voice set")

    # Find all completed shots except the reference
    result = await session.execute(
        select(Shot).where(
            Shot.project_id == project_id,
            Shot.status == ShotStatus.COMPLETED.value,
            Shot.shot_id != project.reference_voice_shot_id,
        )
    )
    shots = result.scalars().all()

    if not shots:
        raise HTTPException(status_code=400, detail="No eligible shots to convert")

    shot_ids = []
    for shot in shots:
        shot.vc_status = "converting"
        shot.vc_error_message = None
        session.add(shot)
        shot_ids.append(shot.shot_id)
    await session.commit()

    arq = await _get_arq_redis(redis)
    await arq.enqueue_job(
        "run_voice_convert_batch", project_id, shot_ids, f"user:{user}"
    )

    return {"status": "queued", "shot_ids": shot_ids}


@router.post("/projects/{project_id}/shots/{shot_id}/voice-revert")
async def voice_revert_shot(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Revert a shot's voice conversion back to original audio."""
    await _get_project_or_404(project_id, session)

    result = await session.execute(
        select(Shot).where(
            Shot.project_id == project_id, Shot.shot_id == shot_id
        )
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    if shot.vc_status != "done":
        raise HTTPException(status_code=400, detail="Shot has not been voice-converted")

    # Restore pre-VC video
    pre_vc = shot_pre_vc_video_path(project_id, shot_id)
    if pre_vc.exists():
        video_path = Path(shot.video_path)
        shutil.copy2(str(pre_vc), str(video_path))

    shot.vc_status = None
    shot.vc_error_message = None
    session.add(shot)
    await session.commit()

    return {
        "shot_id": shot_id,
        "vc_status": None,
        "video_path": to_media_url(shot.video_path),
    }
