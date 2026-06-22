"""Pipeline API routes for video generation workflow."""

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Header, UploadFile, File
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import observability
from app.config import settings
from app.db import get_session
from app.main import get_redis
from app.models.project import Project, Shot, ReferenceImage
from app.models.schemas import (
    ProjectResponse, StoryboardUpdate, ShotUpdate, ShotAiEditRequest,
    ShotTrimRequest, RegenerateShotsRequest, PipelineActionResponse,
    ReferenceVoiceRequest, ExportRequest,
)
from app.services.state_machine import (
    ProjectStatus, ShotStatus,
    transition_project_status, InvalidTransitionError
)
from app.services.storage import (
    storyboard_path, archived_storyboard_path, shot_custom_frames_dir, to_media_url,
    shot_pre_vc_video_path, shot_audio_original_path, shot_audio_vc_path,
    shot_pre_cc_last_frame_path,
)
from app.services.events import publish_event

router = APIRouter()


def _shot_needs_tail_frame(shot: Shot) -> bool:
    """Check if a shot should go through tail frame generation before video."""
    if shot.tf_confirmed:
        return False  # already confirmed
    if shot.skip_tail_frame:
        return False  # user explicitly chose to skip tail frame, use first frame only
    # Shot 1 or connected shots need tail frames
    return shot.shot_id == 1 or shot.align_with_previous


def _reset_tail_frame(shot: Shot, *, skip: bool) -> None:
    """Clear a shot's tail-frame state in one place.

    ``skip=True``  → 中性删除：清空尾帧，不再自动走尾帧流程（仅用首帧出视频）。
    ``skip=False`` → 重新启用尾帧流程（重新生成）。
    """
    shot.tf_status = None
    shot.tf_confirmed = False
    shot.target_last_frame_path = None
    shot.tf_error_message = None
    shot.skip_tail_frame = skip


async def _enqueue_next_shot_task(
    project_id: str, session: AsyncSession, arq, user: str
) -> str:
    """Pick the next pending shot and enqueue the right task (tail frame or video).

    Returns the enqueued job name.
    """
    result = await session.execute(
        select(Shot)
        .where(
            Shot.project_id == project_id,
            Shot.status.in_([ShotStatus.PENDING.value, ShotStatus.FAILED.value]),
        )
        .order_by(Shot.shot_id)
        .limit(1)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        return "none"

    if _shot_needs_tail_frame(shot):
        shot.tf_status = "generating"
        shot.tf_confirmed = False
        session.add(shot)
        await session.commit()
        await arq.enqueue_job(
            "run_tail_frame_pipeline", project_id, shot.shot_id, f"user:{user}"
        )
        return "run_tail_frame_pipeline"
    else:
        await arq.enqueue_job("run_shot_pipeline", project_id, f"user:{user}")
        return "run_shot_pipeline"


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

    # Reset all shots to PENDING and clear tail frame state
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id)
    )
    for shot in result.scalars().all():
        shot.status = ShotStatus.PENDING.value
        shot.error_message = None
        shot.motion_prompt = None
        shot.tf_status = None
        shot.tf_error_message = None
        shot.tf_confirmed = False
        shot.target_last_frame_path = None
        session.add(shot)
    await session.commit()

    # Enqueue tail frame or video pipeline for the first pending shot
    arq = await _get_arq_redis(redis)
    job = await _enqueue_next_shot_task(project_id, session, arq, user)

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

    # Reset specified shots to PENDING and clear post-processing state.
    # Keep motion_prompt / first_frame_path so the re-run reuses the existing
    # director take and first frame instead of regenerating them.
    # The confirmed target tail frame is kept when its file still exists so
    # "连续" shots still converge on the same endpoint.
    result = await session.execute(
        select(Shot).where(
            Shot.project_id == project_id,
            Shot.shot_id.in_(body.shot_ids),
        )
    )
    for shot in result.scalars().all():
        shot.status = ShotStatus.PENDING.value
        shot.error_message = None
        shot.video_path = None
        shot.last_frame_path = None
        shot.vc_status = None
        shot.vc_error_message = None
        shot.cc_status = None
        shot.cc_error_message = None

        has_valid_tail_frame = (
            shot.target_last_frame_path
            and Path(shot.target_last_frame_path).exists()
        )
        logger.info(
            "regenerate shot %d: target_last_frame_path=%s exists=%s has_valid=%s",
            shot.shot_id,
            shot.target_last_frame_path,
            Path(shot.target_last_frame_path).exists() if shot.target_last_frame_path else "N/A",
            has_valid_tail_frame,
        )
        if has_valid_tail_frame:
            # Keep the confirmed target tail frame for continuity.
            shot.tf_status = "done"
            shot.tf_error_message = None
            shot.tf_confirmed = True
        else:
            shot.tf_status = None
            shot.tf_error_message = None
            shot.tf_confirmed = False
            shot.target_last_frame_path = None

        session.add(shot)
    await session.commit()

    # Enqueue tail frame or video pipeline for the first pending shot
    arq = await _get_arq_redis(redis)
    job = await _enqueue_next_shot_task(project_id, session, arq, user)

    return {"status": "queued", "message": "Shot regeneration queued"}


@router.post("/projects/{project_id}/continue-generation", status_code=202)
async def continue_generation(
    project_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """Continue generating the next pending shot (approve current, generate next).

    Unlike approve-script, this does NOT auto-generate tail frames.
    The user is expected to have already generated and confirmed tail frames
    for shots that need them before clicking continue.
    """
    project = await _get_project_or_404(project_id, session)

    if project.status != ProjectStatus.SHOT_REVIEW.value:
        raise HTTPException(status_code=409, detail="Project must be in shot_review status")

    # Find next pending shot
    result = await session.execute(
        select(Shot)
        .where(
            Shot.project_id == project_id,
            Shot.status.in_([ShotStatus.PENDING.value, ShotStatus.FAILED.value]),
        )
        .order_by(Shot.shot_id)
        .limit(1)
    )
    next_shot = result.scalar_one_or_none()
    if not next_shot:
        raise HTTPException(status_code=400, detail="No pending shots to generate")

    # Validate: if the shot needs a tail frame, it must be confirmed already
    if _shot_needs_tail_frame(next_shot):
        raise HTTPException(
            status_code=400,
            detail=f"镜头 #{next_shot.shot_id} 需要先生成并确认尾帧",
        )

    try:
        await transition_project_status(
            project, ProjectStatus.SHOT_GENERATING, f"user:{user}", session, redis
        )
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    # Directly enqueue video generation — no auto tail frame generation
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
    if body.auto_trim is not None:
        shot.auto_trim = body.auto_trim

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
        "auto_trim": shot.auto_trim,
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
        async with observability.project_context(project_id, "api-shot-editor-edit"):
            result = await run_shot_editor(**editor_kwargs)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI generation failed: {e}")

    return result


@router.post("/projects/{project_id}/shots/{shot_id}/ai-edit-prompt")
async def ai_edit_motion_prompt(
    project_id: str,
    shot_id: int,
    body: ShotAiEditRequest,
):
    """Use AI to revise a shot's motion prompt based on a user instruction."""
    from app.agents.llm import GeminiProvider
    from app.db import AsyncSession as session_factory

    async with session_factory() as session:
        result = await session.execute(
            select(Shot).where(
                Shot.project_id == project_id, Shot.shot_id == shot_id
            )
        )
        shot = result.scalar_one_or_none()
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        if not shot.motion_prompt:
            raise HTTPException(status_code=400, detail="Shot has no motion prompt yet")

        current_prompt = shot.motion_prompt
        shot_type = shot.shot_type
        text = shot.text
        duration = shot.shot_duration

    provider = GeminiProvider(
        project=settings.gemini_project, location=settings.gemini_location
    )
    system = (
        "You are a professional video motion director. The user gives you an existing "
        "Veo motion prompt and a revision instruction.\n"
        "Revise the prompt according to the instruction. Output ONLY the revised full "
        "motion prompt in English. No explanation.\n"
        "Rules:\n"
        "- Never describe character appearance (face, gender, clothing, colors)\n"
        "- 100% focus on motion, camera movement, expression changes\n"
        "- If there is dialogue, keep the lip-sync instructions\n"
        "- All visible body parts must remain visible throughout the shot — no unmotivated "
        "disappearances; if a body part exits frame, describe the exit trajectory\n"
        "- The output MUST be in English even if the input is in another language"
    )
    user_msg = (
        f"Shot type: {shot_type}\n"
        f"Duration: {duration}s\n"
        f"Dialogue: {text or 'None'}\n\n"
        f"Current motion prompt:\n{current_prompt}\n\n"
        f"Revision instruction: {body.instruction}\n\n"
        f"Output the revised full motion prompt in English:"
    )

    try:
        async with observability.project_context(project_id, "api-regenerate-motion"):
            new_prompt = await provider.generate_text(
                model=settings.gemini_director_model,
                system_prompt=system,
                user_message=user_msg,
                temperature=0.7,
                operation="api-pipeline-regenerate-motion",
            )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI generation failed: {e}")

    return {"motion_prompt": new_prompt}


@router.post("/projects/{project_id}/shots/{shot_id}/rewrite-prompt")
async def rewrite_motion_prompt(
    project_id: str,
    shot_id: int,
):
    """Re-generate a shot's motion prompt from scratch using the Director agent."""
    from app.agents.director import run_director as run_director_agent
    from app.agents.llm import GeminiProvider
    from app.db import AsyncSession as session_factory

    async with session_factory() as session:
        result = await session.execute(
            select(Shot).where(
                Shot.project_id == project_id, Shot.shot_id == shot_id
            )
        )
        shot = result.scalar_one_or_none()
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")

        shot_type = shot.shot_type
        visual_description = shot.visual_description
        text = shot.text
        duration = shot.shot_duration

    provider = GeminiProvider(
        project=settings.gemini_project, location=settings.gemini_location
    )

    try:
        async with observability.project_context(project_id, "api-rewrite-motion"):
            new_prompt = await run_director_agent(
                shot_id=shot_id,
                shot_type=shot_type,
                visual_description=visual_description,
                text=text,
                duration=duration,
                llm_provider=provider,
            )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Director agent failed: {e}")

    return {"motion_prompt": new_prompt}


@router.post("/projects/{project_id}/export", status_code=202)
async def export_project(
    project_id: str,
    body: ExportRequest = ExportRequest(),
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
    await arq.enqueue_job("run_merger", project_id, f"user:{user}", body.crossfade_duration)

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

    # Reset any in-progress shots back to pending
    result = await session.execute(
        select(Shot).where(
            Shot.project_id == project_id,
            Shot.status.in_(["video_generating", "prompt_generating"]),
        )
    )
    for shot in result.scalars().all():
        shot.status = ShotStatus.PENDING.value
        session.add(shot)

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


@router.post("/projects/{project_id}/shots/{shot_id}/generate-tail-frame", status_code=202)
async def generate_tail_frame(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """Generate a target tail frame for a shot (director + tail frame generation)."""
    project = await _get_project_or_404(project_id, session)

    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    # Transition to SHOT_GENERATING
    try:
        await transition_project_status(
            project, ProjectStatus.SHOT_GENERATING, f"user:{user}", session, redis
        )
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    _reset_tail_frame(shot, skip=False)  # re-enable tail frame flow on re-generate
    shot.tf_status = "generating"
    session.add(shot)
    await session.commit()

    arq = await _get_arq_redis(redis)
    await arq.enqueue_job("run_tail_frame_pipeline", project_id, shot_id, f"user:{user}")

    return {"status": "queued", "shot_id": shot_id}


@router.post("/projects/{project_id}/shots/{shot_id}/confirm-tail-frame", status_code=202)
async def confirm_tail_frame(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """Confirm tail frame and start video generation for this shot."""
    project = await _get_project_or_404(project_id, session)

    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    if shot.tf_status != "done":
        raise HTTPException(status_code=400, detail="Tail frame not generated yet")

    if not shot.target_last_frame_path:
        raise HTTPException(status_code=400, detail="No target tail frame exists")

    shot.tf_confirmed = True
    session.add(shot)
    await session.commit()

    # Transition to SHOT_GENERATING and enqueue video generation
    try:
        await transition_project_status(
            project, ProjectStatus.SHOT_GENERATING, f"user:{user}", session, redis
        )
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    arq = await _get_arq_redis(redis)
    await arq.enqueue_job("run_shot_pipeline", project_id, f"user:{user}", shot_id)

    return {
        "shot_id": shot_id,
        "tf_confirmed": True,
        "target_last_frame_path": to_media_url(shot.target_last_frame_path),
    }


@router.post("/projects/{project_id}/shots/{shot_id}/delete-tail-frame")
async def delete_tail_frame(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Delete a shot's target tail frame, returning it to a neutral state.

    Clears all tail frame state, sets ``skip_tail_frame = True`` (so a later video
    generation uses first-frame-only), and removes the ``target_last_frame.png`` file.
    Does NOT transition the project or enqueue video generation — the shot simply
    returns to a state where the user can re-generate a tail frame or proceed.
    The "生成尾帧" entry re-appears (``tf_status`` is cleared) and re-generating
    overwrites via ``skip_tail_frame = False``.
    """
    await _get_project_or_404(project_id, session)

    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    # Prevent deleting while tail frame is being actively generated
    if shot.tf_status == "generating":
        raise HTTPException(
            status_code=409,
            detail="Tail frame is currently being generated; wait for it to complete",
        )

    # Clear all tail-frame state; skip=True keeps the shot on first-frame-only video
    _reset_tail_frame(shot, skip=True)
    session.add(shot)
    await session.commit()

    # Remove the physical tail-frame file (deterministic canonical path)
    from app.services.storage import shot_target_last_frame_path
    shot_target_last_frame_path(project_id, shot_id).unlink(missing_ok=True)

    return {
        "shot_id": shot_id,
        "skip_tail_frame": True,
        "tf_status": None,
    }


@router.post("/projects/{project_id}/shots/{shot_id}/extract-tail-frame")
async def extract_tail_frame(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Use the video's actual last frame as the target tail frame."""
    await _get_project_or_404(project_id, session)

    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")
    if not shot.last_frame_path:
        raise HTTPException(status_code=400, detail="Shot has no last frame")

    src = Path(shot.last_frame_path)
    if not src.exists():
        raise HTTPException(status_code=400, detail="Last frame file not found")

    # Copy last_frame.png → target_last_frame.png
    from app.services.storage import shot_target_last_frame_path
    dest = shot_target_last_frame_path(project_id, shot_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dest))

    shot.target_last_frame_path = str(dest)
    shot.tf_status = "done"
    shot.tf_error_message = None
    shot.tf_confirmed = False
    session.add(shot)
    await session.commit()

    return {
        "shot_id": shot_id,
        "target_last_frame_path": to_media_url(str(dest)),
        "tf_status": "done",
    }


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

    # Save new files (append)
    for upload in files:
        content = await upload.read()
        safe_name = Path(upload.filename).name if upload.filename else "image.png"
        image_id = str(_uuid.uuid4())[:8]
        dest_path = dest_dir / f"{image_id}_{safe_name}"
        dest_path.write_bytes(content)
        existing_paths.append(str(dest_path))

    # Always store as reference_images so they are passed as object refs
    all_paths = existing_paths
    shot.custom_first_frame_path = None
    shot.custom_reference_paths = json.dumps(all_paths) if all_paths else None

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

        if index < 0 or index >= len(all_paths):
            raise HTTPException(status_code=400, detail="Invalid index")

        # Delete file
        removed = Path(all_paths.pop(index))
        removed.unlink(missing_ok=True)

        # Update DB
        shot.custom_first_frame_path = None
        shot.custom_reference_paths = json.dumps(all_paths) if all_paths else None
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

    all_paths = json.loads(shot.custom_reference_paths) if shot.custom_reference_paths else []

    if len(order) != len(all_paths):
        raise HTTPException(status_code=400, detail="Order length mismatch")

    reordered = [all_paths[i] for i in order]

    shot.custom_first_frame_path = None
    shot.custom_reference_paths = json.dumps(reordered) if reordered else None

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

    info = get_video_info(shot.video_path)
    backup = Path(shot.video_path).with_name("output_original.mp4")
    info["has_backup"] = backup.exists()
    return info


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

    # Backup original on first trim
    backup = video_path.with_name("output_original.mp4")
    if not backup.exists():
        video_path.rename(backup)
    source = str(backup)

    # Trim (use frame count, not time — avoids float rounding ±1 frame)
    trim_video(source, str(video_path), body.end_frame)

    # Re-extract last frame
    if shot.last_frame_path:
        extract_last_frame(str(video_path), shot.last_frame_path)

    # Reset character calibration since last frame changed
    shot.cc_status = None
    shot.cc_error_message = None

    # Remove stale pre-CC backup so next calibration creates a fresh one
    from app.services.storage import shot_pre_cc_last_frame_path
    pre_cc = shot_pre_cc_last_frame_path(project_id, shot_id)
    if pre_cc.exists():
        pre_cc.unlink()

    # Remove stale pre-VC backup — audio length no longer matches trimmed video
    from app.services.storage import shot_pre_vc_video_path
    pre_vc = shot_pre_vc_video_path(project_id, shot_id)
    if pre_vc.exists():
        pre_vc.unlink()
    shot.vc_status = None
    shot.vc_error_message = None

    ts = int(datetime.utcnow().timestamp())
    await session.commit()

    return {
        "video_path": to_media_url(str(video_path)),
        "last_frame_path": to_media_url(shot.last_frame_path),
        "version": ts,
        **get_video_info(str(video_path)),
    }


@router.post("/projects/{project_id}/shots/{shot_id}/restore-trim")
async def restore_trim(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Restore the original video before any trimming."""
    from app.agents.video_trimmer import get_video_info
    from app.agents.frame_porter import extract_last_frame

    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot or not shot.video_path:
        raise HTTPException(status_code=404, detail="Shot or video not found")

    video_path = Path(shot.video_path)
    backup = video_path.with_name("output_original.mp4")
    if not backup.exists():
        raise HTTPException(status_code=404, detail="No backup found — video was never trimmed")

    # Restore original
    import shutil
    shutil.copy2(str(backup), str(video_path))
    backup.unlink()

    # Re-extract last frame
    if shot.last_frame_path:
        extract_last_frame(str(video_path), shot.last_frame_path)

    # Reset character calibration since last frame changed
    shot.cc_status = None
    shot.cc_error_message = None

    from app.services.storage import shot_pre_cc_last_frame_path
    pre_cc = shot_pre_cc_last_frame_path(project_id, shot_id)
    if pre_cc.exists():
        pre_cc.unlink()

    # Remove stale pre-VC backup — audio length no longer matches restored video
    from app.services.storage import shot_pre_vc_video_path
    pre_vc = shot_pre_vc_video_path(project_id, shot_id)
    if pre_vc.exists():
        pre_vc.unlink()
    shot.vc_status = None
    shot.vc_error_message = None

    ts = int(datetime.utcnow().timestamp())
    await session.commit()

    return {
        "video_path": to_media_url(str(video_path)),
        "last_frame_path": to_media_url(shot.last_frame_path),
        "version": ts,
        **get_video_info(str(video_path)),
    }


@router.post("/projects/{project_id}/shots/{shot_id}/align-tail-frame")
async def align_tail_frame(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Auto-trim video to the frame that best matches the target tail frame (SSIM)."""
    from app.agents.video_trimmer import find_best_tail_frame, get_video_info, trim_video
    from app.agents.frame_porter import extract_last_frame

    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot or not shot.video_path:
        raise HTTPException(status_code=404, detail="Shot or video not found")
    if not shot.target_last_frame_path:
        raise HTTPException(status_code=400, detail="No target tail frame for this shot")

    best_frames = find_best_tail_frame(shot.video_path, shot.target_last_frame_path)
    if best_frames is None:
        # Already optimal — return current info without trimming
        info = get_video_info(shot.video_path)
        return {
            "video_path": to_media_url(shot.video_path),
            "last_frame_path": to_media_url(shot.last_frame_path),
            "version": int(datetime.utcnow().timestamp()),
            "aligned_to_frame": info["total_frames"],
            **info,
        }

    video_path = Path(shot.video_path)

    # Backup original on first trim (same logic as manual trim)
    backup = video_path.with_name("output_original.mp4")
    if not backup.exists():
        video_path.rename(backup)
    source = str(backup)

    trim_video(source, str(video_path), best_frames)

    # Re-extract last frame
    if shot.last_frame_path:
        extract_last_frame(str(video_path), shot.last_frame_path)

    # Reset character calibration since last frame changed
    shot.cc_status = None
    shot.cc_error_message = None
    pre_cc = shot_pre_cc_last_frame_path(project_id, shot_id)
    if pre_cc.exists():
        pre_cc.unlink()

    # Remove stale pre-VC backup
    pre_vc = shot_pre_vc_video_path(project_id, shot_id)
    if pre_vc.exists():
        pre_vc.unlink()
    shot.vc_status = None
    shot.vc_error_message = None

    ts = int(datetime.utcnow().timestamp())
    await session.commit()

    return {
        "video_path": to_media_url(str(video_path)),
        "last_frame_path": to_media_url(shot.last_frame_path),
        "version": ts,
        "aligned_to_frame": best_frames,
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
        "run_voice_convert", project_id, shot_id, f"user:{user}",
        _queue_name="arq:vc",
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
        "run_voice_convert_batch", project_id, shot_ids, f"user:{user}",
        _queue_name="arq:vc",
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

    ts = int(datetime.utcnow().timestamp())
    return {
        "shot_id": shot_id,
        "vc_status": None,
        "video_path": to_media_url(shot.video_path),
        "version": ts,
    }


# ============== Character Calibration ==============


@router.post("/projects/{project_id}/shots/{shot_id}/character-calibrate", status_code=202)
async def character_calibrate_shot(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """Calibrate a shot's last frame to match character reference images."""
    project = await _get_project_or_404(project_id, session)

    # Validate project has character reference images
    ref_result = await session.execute(
        select(ReferenceImage).where(
            ReferenceImage.project_id == project_id,
            ReferenceImage.kind == "character",
        )
    )
    if not ref_result.scalars().first():
        raise HTTPException(status_code=400, detail="No character reference images")

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
    if not shot.last_frame_path:
        raise HTTPException(status_code=400, detail="Shot has no last frame")

    shot.cc_status = "calibrating"
    shot.cc_error_message = None
    session.add(shot)
    await session.commit()

    arq = await _get_arq_redis(redis)
    await arq.enqueue_job(
        "run_character_calibrate", project_id, shot_id, f"user:{user}",
    )

    return {"status": "queued", "shot_id": shot_id}


@router.post("/projects/{project_id}/character-calibrate-all", status_code=202)
async def character_calibrate_all(
    project_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """Calibrate all completed shots' last frames to match character references."""
    project = await _get_project_or_404(project_id, session)

    # Validate project has character reference images
    ref_result = await session.execute(
        select(ReferenceImage).where(
            ReferenceImage.project_id == project_id,
            ReferenceImage.kind == "character",
        )
    )
    if not ref_result.scalars().first():
        raise HTTPException(status_code=400, detail="No character reference images")

    # Find all completed shots with last frames
    result = await session.execute(
        select(Shot).where(
            Shot.project_id == project_id,
            Shot.status == ShotStatus.COMPLETED.value,
            Shot.last_frame_path.isnot(None),
        )
    )
    shots = result.scalars().all()

    if not shots:
        raise HTTPException(status_code=400, detail="No eligible shots to calibrate")

    shot_ids = []
    for shot in shots:
        shot.cc_status = "calibrating"
        shot.cc_error_message = None
        session.add(shot)
        shot_ids.append(shot.shot_id)
    await session.commit()

    arq = await _get_arq_redis(redis)
    await arq.enqueue_job(
        "run_character_calibrate_batch", project_id, shot_ids, f"user:{user}",
    )

    return {"status": "queued", "shot_ids": shot_ids}


@router.post("/projects/{project_id}/shots/{shot_id}/character-calibrate-revert")
async def character_calibrate_revert(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Revert a shot's character calibration back to the original last frame."""
    await _get_project_or_404(project_id, session)

    result = await session.execute(
        select(Shot).where(
            Shot.project_id == project_id, Shot.shot_id == shot_id
        )
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    if shot.cc_status != "done":
        raise HTTPException(status_code=400, detail="Shot has not been character-calibrated")

    # Restore pre-CC last frame
    pre_cc = shot_pre_cc_last_frame_path(project_id, shot_id)
    if pre_cc.exists() and shot.last_frame_path:
        shutil.copy2(str(pre_cc), shot.last_frame_path)

    shot.cc_status = None
    shot.cc_error_message = None
    session.add(shot)
    await session.commit()

    ts = int(datetime.utcnow().timestamp())
    return {
        "shot_id": shot_id,
        "cc_status": None,
        "last_frame_path": to_media_url(shot.last_frame_path),
        "version": ts,
    }
