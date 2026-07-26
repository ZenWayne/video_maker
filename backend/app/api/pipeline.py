"""Pipeline API routes for video generation workflow."""

import hashlib
import json
import logging
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
    ProjectResponse, StoryboardUpdate, StoryboardReplace, ShotUpdate, ShotAiEditRequest,
    ShotTrimRequest, RegenerateShotsRequest, PipelineActionResponse,
    JoinPreviewRequest,
)
from app.services.first_frame import pick_first_frame
from app.services.state_machine import (
    ProjectStatus, ShotStatus,
    transition_project_status, InvalidTransitionError
)
# 注：原来的本地路径 import 块一次性 import 了 10 个已被 Task 5 删除的函数
# （storyboard_path/archived_storyboard_path/shot_custom_frames_dir/
# shot_pre_vc_video_path/shot_audio_original_path/shot_audio_vc_path/
# shot_pre_cc_last_frame_path/join_preview_path/shot_dir/shot_source_path），
# 导致整个模块 ImportError。Task 8 负责 trim/restore-trim/align-tail-frame
# （已重写为 workspace()+COS key，不再需要 shot_dir/shot_source_path/
# shot_pre_cc_last_frame_path）。Task 10（上传链路）已修复全部
# shot_custom_frames_dir/shot_dir 的上传/拷贝/删除端点调用点（upload-first-frame/
# upload-tail-frame/reference-images/extract-first-frame/extract-last-frame/
# extract-tail-frame/use-prev-last-frame/delete-tail-frame/first-frame）——全部
# 改为 workspace()+COS key 或 object_store.copy/delete(_prefix)。Task 11（导出
# 合并/连贯性预览/项目删除/storyboard）已修复：storyboard_path/
# archived_storyboard_path（regenerate-script/put-storyboard/reset 三处归档+
# 改写）改用 app.services.storyboard 的 write_storyboard/archive_storyboard/
# read_storyboard；join_preview_path 改用 workspace()+join_preview_key；
# put_storyboard 里残留的 shot_dir 清理改用
# object_store.delete_prefix(shot_prefix(...))。Task 12（读路径）已修复最后一批
# 只读展示端点（video-info/waveform/filmstrip/detect-silence/detect-speech-start）：
# 这些端点以前靠 shot_source_path()/pristine_video_path() 在本地磁盘上区分「源片
# vs 派生文件」，但 Task 8 起 trim/restore-trim/align-tail-frame 已经是纯 metadata
# 操作（shot.video_path 指向的源对象永不改写，VC 只写 vc_audio_path）——也就是说
# shot.video_path 本身恒为源片，不再需要任何解析函数；改为 workspace().fetch()
# 直接下载 shot.video_path 到本地跑 ffprobe/ffmpeg（见 _fetch_dialog_source）。
# filmstrip 的确定性缓存也从本地 shot_dir 迁到了 COS（fname 相同即复用，count
# 变化时清理旧对象）。CC 还原读的是 pristine_last_frame_key 这一 DB 列，不是
# 函数，不受影响。shot_pre_vc_video_path/shot_audio_original_path/
# shot_audio_vc_path 是死 import，正文从未使用，也已随上面的 import 块清理。
from app.services.storage import (
    to_media_url, ts_uuid_name, shot_key, shot_prefix, join_preview_key,
)
from app.services import object_store
from app.services.workspace import workspace, ensure_free_space
from app.services.storyboard import write_storyboard, archive_storyboard, read_storyboard
from app.services.events import publish_event

router = APIRouter()


def _reset_tail_frame(shot: Shot) -> None:
    """Clear a shot's tail-frame state in one place.

    Clears tf_status, target_last_frame_path, and tf_error_message.
    Path-as-truth: a tail frame is used iff target_last_frame_path is set
    (decided by resolve_tail_frame in worker).
    """
    shot.tf_status = None
    shot.tf_confirmed = False
    shot.target_last_frame_path = None
    shot.tf_error_message = None


async def _enqueue_next_shot_task(
    project_id: str, session: AsyncSession, arq, user: str
) -> str:
    """Pick the next pending shot and enqueue the video pipeline task.

    Path-as-truth: tail frame use is decided inside the worker (resolve_tail_frame).
    Auto tail-frame generation is no longer triggered here — use the explicit
    generate-tail-frame endpoint instead.

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
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    await archive_storyboard(project_id, ts)

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
    sb_data = await read_storyboard(project.storyboard_path)
    storyboard = None
    if sb_data:
        try:
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


@router.put("/projects/{project_id}/storyboard", response_model=ProjectResponse)
async def put_storyboard(
    project_id: str,
    body: StoryboardReplace,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Full-replace storyboard: upsert shots by shot_id, delete missing, rewrite storyboard.json.

    Only allowed in SCRIPT_REVIEW (pre-render): no generated material files at stake.
    """
    project = await _get_project_or_404(project_id, session)

    if project.status != ProjectStatus.SCRIPT_REVIEW.value:
        raise HTTPException(
            status_code=409,
            detail="Project must be in script_review status to replace storyboard",
        )

    result = await session.execute(select(Shot).where(Shot.project_id == project_id))
    existing = {s.shot_id: s for s in result.scalars().all()}
    payload_ids = {item.shot_id for item in body.shots}

    # Delete shots absent from the payload + remove any leftover output objects
    # (CLAUDE.md audit). Commit the DB deletion FIRST and only then clear COS —
    # deleting the COS objects before the DB row is durably gone would leave a
    # window where a crash/rollback resurrects a Shot row pointing at objects
    # that no longer exist (the DB row must never outlive what it references).
    removed_shot_ids = [sid for sid in existing if sid not in payload_ids]
    for shot_id in removed_shot_ids:
        await session.delete(existing[shot_id])
    if removed_shot_ids:
        await session.commit()
        for shot_id in removed_shot_ids:
            await object_store.delete_prefix(shot_prefix(project_id, shot_id))

    # Upsert shots present in the payload.
    for item in body.shots:
        shot = existing.get(item.shot_id)
        if shot is None:
            shot = Shot(project_id=project_id, shot_id=item.shot_id)
            session.add(shot)
        shot.text = item.text
        shot.shot_type = item.shot_type
        shot.visual_description = item.visual_description
        shot.shot_duration = item.shot_duration
        shot.align_with_previous = item.align_with_previous
        shot.reference_image_hint = item.reference_image_hint

    project.scene_overview = body.scene_overview

    # Rewrite storyboard.json to match (DB is source of truth).
    sb_key = await write_storyboard(
        project_id, body.scene_overview, [item.model_dump() for item in body.shots]
    )
    project.storyboard_path = sb_key
    project.updated_at = datetime.utcnow()
    session.add(project)
    await session.commit()
    await session.refresh(project)

    from app.models.schemas import Storyboard
    sb_data = await read_storyboard(project.storyboard_path)
    storyboard = None
    if sb_data:
        try:
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
    # Keep motion_prompt so the re-run reuses the existing (expensive) director
    # take instead of regenerating it. The first frame is never stored: the worker
    # always re-resolves it from custom_first_frame_path / continuity via
    # pick_first_frame, so a fresh 首帧 upload is always honored.
    # Path-as-truth: target_last_frame_path is left EXACTLY as stored.
    # Whether the tail frame is actually used is decided by the worker
    # (resolve_tail_frame checks file presence at run time).
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
        # target_last_frame_path and tf_confirmed are intentionally NOT touched here.
        # The worker decides whether to use the tail frame based on file presence.
        session.add(shot)
    await session.commit()

    # Enqueue video pipeline for the first pending shot
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

    Path-as-truth: tail frame use is decided by the worker (resolve_tail_frame).
    No tail-frame confirmation gate is enforced here — the worker picks up any
    target_last_frame_path that is already set.
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
        "video_path": shot.video_path,
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
        object_ref_paths = (
            json.loads(shot.custom_reference_paths)
            if shot.custom_reference_paths else None
        )

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
                reference_image_paths=object_ref_paths,
            )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Director agent failed: {e}")

    return {"motion_prompt": new_prompt}


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


@router.post("/projects/{project_id}/join-preview")
async def join_preview(
    project_id: str,
    body: JoinPreviewRequest,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """临时把选中的 shot 纯拼接成一条预览视频，用于检测连贯性。同步执行。"""
    from app.agents.merger import merge_shots
    from app.agents.effective_clip import ClipSpec, effective_clip_paths

    await _get_project_or_404(project_id, session)

    if len(body.shot_ids) < 2:
        raise HTTPException(
            status_code=400, detail="至少选择 2 个镜头才能拼接预览"
        )

    result = await session.execute(
        select(Shot).where(
            Shot.project_id == project_id,
            Shot.shot_id.in_(body.shot_ids),
        )
    )
    shots_by_id = {s.shot_id: s for s in result.scalars().all()}

    ordered_shots: list = []
    for sid in body.shot_ids:
        shot = shots_by_id.get(sid)
        if shot is None:
            raise HTTPException(status_code=400, detail=f"镜头 {sid} 不存在")
        if shot.status != ShotStatus.COMPLETED.value:
            raise HTTPException(
                status_code=400, detail=f"镜头 {sid} 尚未完成，无法预览"
            )
        if not shot.video_path or not await object_store.exists(shot.video_path):
            raise HTTPException(
                status_code=400, detail=f"镜头 {sid} 缺少视频文件"
            )
        ordered_shots.append(shot)

    # Apply the non-destructive EDL (trim + VC) before stitching, so the
    # continuity preview reflects the trimmed clips — not the full source.
    # Precheck real object sizes before fetching (same rationale as export
    # merge): a handful of shots is usually small, but skipping the precheck
    # still risks an opaque ffmpeg failure on a disk-constrained host.
    keys = [s.video_path for s in ordered_shots]
    keys += [s.vc_audio_path for s in ordered_shots if s.vc_audio_path]
    total = sum([await object_store.size(k) for k in keys])
    await ensure_free_space(int(total * 2.2))

    try:
        async with workspace() as ws:
            specs: list[ClipSpec] = []
            for i, shot in enumerate(ordered_shots):
                # Distinct name per shot: fetching several shots into ONE
                # workspace needs it (ws.fetch raises on two different keys
                # sharing a default local name — every shot's video is
                # "output_<ts>_<uuid>.mp4"-shaped).
                local_video = await ws.fetch(shot.video_path, name=f"part_{i:04d}.mp4")
                local_vc = None
                if shot.vc_audio_path:
                    local_vc = str(await ws.fetch(shot.vc_audio_path, name=f"vc_{i:04d}.wav"))
                specs.append(ClipSpec(
                    local_video_path=str(local_video),
                    trim_frames=shot.trim_frames,
                    local_vc_audio_path=local_vc,
                    audio_head_mute_frames=shot.audio_head_mute_frames,
                ))

            clip_paths = effective_clip_paths(specs, str(ws.root))
            out = ws.path("join_preview.mp4")
            merge_shots(clip_paths, str(out))
            # cache-busting：用输出文件修改时间(纳秒)，避免浏览器/video 缓存旧预览。
            # 必须在 workspace 退出（自动删除临时文件）前读取。
            bust = out.stat().st_mtime_ns
            key = await ws.publish(out, join_preview_key(project_id))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"拼接失败: {e}")

    media_url = to_media_url(key)
    return {"preview_url": f"{media_url}?t={bust}"}


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
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    await archive_storyboard(project_id, ts)

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
    """[Deprecated wrapper] 创建 auto 尾帧候选（新入口：POST .../image-candidates）。"""
    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    from app.models.project import ImageCandidate
    cand = ImageCandidate(
        project_id=project_id, shot_pk=shot.id, shot_id=shot_id,
        slot="tail_frame", status="generating", prompt_source="auto",
    )
    session.add(cand)
    await session.commit()
    await session.refresh(cand)

    arq = await _get_arq_redis(redis)
    await arq.enqueue_job("run_image_candidate", project_id, shot_id, cand.id, f"user:{user}")
    return {"status": "queued", "shot_id": shot_id, "candidate_id": cand.id}


@router.post("/projects/{project_id}/shots/{shot_id}/generate-first-frame", status_code=202)
async def generate_first_frame(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """[Deprecated wrapper] 创建 auto 首帧候选（新入口：POST .../image-candidates）。"""
    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    from app.models.project import ImageCandidate
    cand = ImageCandidate(
        project_id=project_id, shot_pk=shot.id, shot_id=shot_id,
        slot="first_frame", status="generating", prompt_source="auto",
    )
    session.add(cand)
    await session.commit()
    await session.refresh(cand)

    arq = await _get_arq_redis(redis)
    await arq.enqueue_job("run_image_candidate", project_id, shot_id, cand.id, f"user:{user}")
    return {"status": "queued", "shot_id": shot_id, "candidate_id": cand.id}


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

    Clears target_last_frame_path and tf_status (path-as-truth: the worker
    decides to use a tail frame only when target_last_frame_path is set).
    Removes the file at the DB-stored path so uploaded/extracted frames (which
    use ts_uuid filenames) are cleaned up correctly — not just the canonical name.
    Does NOT transition the project or enqueue video generation.
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

    # Capture the stored key BEFORE clearing — needed for the COS delete below
    old_key = shot.target_last_frame_path

    # Clear all tail-frame state (path-as-truth: empty path = no tail frame)
    _reset_tail_frame(shot)
    session.add(shot)
    await session.commit()

    # Remove the COS object at the DB-stored key (covers both AI-generated
    # canonical names and ts_uuid filenames from uploaded/extracted frames)
    if old_key:
        await object_store.delete(old_key)

    return {
        "shot_id": shot_id,
        "target_last_frame_path": None,
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

    src_key = shot.last_frame_path
    if not await object_store.exists(src_key):
        raise HTTPException(status_code=400, detail="Last frame file not found")

    # Copy last_frame → target_last_frame (server-side COS copy, unique name)
    dest_key = shot_key(project_id, shot_id, ts_uuid_name(Path(src_key).suffix or ".png"))
    await object_store.copy(src_key, dest_key)

    shot.target_last_frame_path = dest_key
    shot.tf_status = "done"
    shot.tf_error_message = None
    shot.tf_confirmed = False
    session.add(shot)
    await session.commit()

    return {
        "shot_id": shot_id,
        "target_last_frame_path": to_media_url(dest_key),
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

    from app.services.storage import shot_custom_frames_prefix

    # Collect existing keys
    existing_paths: list[str] = []
    if shot.custom_reference_paths:
        existing_paths = json.loads(shot.custom_reference_paths)

    # Save new files (append) — stage locally then publish to COS.
    prefix = shot_custom_frames_prefix(project_id, shot_id)
    async with workspace() as ws:
        for idx, upload in enumerate(files):
            content = await upload.read()
            safe_name = Path(upload.filename).name if upload.filename else "image.png"
            image_id = str(_uuid.uuid4())[:8]
            local = ws.path(f"ref_{idx}_{safe_name}")
            local.write_bytes(content)
            key = await ws.publish(local, f"{prefix}{image_id}_{safe_name}")
            existing_paths.append(key)

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

    from app.services.storage import shot_custom_frames_prefix

    removed_key: Optional[str] = None
    delete_all_prefix: Optional[str] = None

    if index is not None:
        # Delete single image by index
        all_paths: list[str] = []
        if shot.custom_reference_paths:
            all_paths = json.loads(shot.custom_reference_paths)

        if index < 0 or index >= len(all_paths):
            raise HTTPException(status_code=400, detail="Invalid index")

        # 素材审计（CLAUDE.md）：先改 DB 解除引用，再删 COS 对象。
        removed_key = all_paths.pop(index)

        # Update DB
        shot.custom_first_frame_path = None
        shot.custom_reference_paths = json.dumps(all_paths) if all_paths else None
    else:
        # Delete all
        delete_all_prefix = shot_custom_frames_prefix(project_id, shot_id)
        shot.custom_first_frame_path = None
        shot.custom_reference_paths = None

    await session.commit()

    if removed_key:
        await object_store.delete(removed_key)
    if delete_all_prefix:
        await object_store.delete_prefix(delete_all_prefix)

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


@router.post("/projects/{project_id}/shots/{shot_id}/upload-first-frame")
async def upload_first_frame(
    project_id: str,
    shot_id: int,
    file: UploadFile = File(...),
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Upload a custom first frame image for a shot (ts_uuid filename)."""
    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    from app.services.storage import shot_custom_frames_prefix

    ext = Path(file.filename or "x.png").suffix or ".png"
    content = await file.read()
    async with workspace() as ws:
        local = ws.path(ts_uuid_name(ext))
        local.write_bytes(content)
        key = await ws.publish(local, f"{shot_custom_frames_prefix(project_id, shot_id)}{local.name}")
    shot.custom_first_frame_path = key
    await session.commit()
    return {"shot_id": shot_id, "custom_first_frame_path": to_media_url(key)}


@router.post("/projects/{project_id}/shots/{shot_id}/upload-tail-frame")
async def upload_tail_frame(
    project_id: str,
    shot_id: int,
    file: UploadFile = File(...),
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Upload a custom tail frame image for a shot (ts_uuid filename, sets tf_status=done)."""
    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    ext = Path(file.filename or "x.png").suffix or ".png"
    content = await file.read()
    async with workspace() as ws:
        local = ws.path(ts_uuid_name(ext))
        local.write_bytes(content)
        key = await ws.publish(local, shot_key(project_id, shot_id, local.name))
    shot.target_last_frame_path = key
    shot.tf_status = "done"
    await session.commit()
    return {
        "shot_id": shot_id,
        "target_last_frame_path": to_media_url(key),
        "tf_status": "done",
    }


@router.post("/projects/{project_id}/shots/{shot_id}/extract-first-frame")
async def extract_first_frame(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Pin the shot's resolved first frame into custom_first_frame_path (ts_uuid filename).

    The source is resolved on demand by the single-source resolver (there is no
    stored first_frame_path): custom_first_frame_path → previous shot's last frame
    → character reference.
    """
    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    from app.services.storage import shot_custom_frames_prefix

    try:
        src = await pick_first_frame(project_id, shot, session)
    except ValueError:
        src = None
    if src is None:
        raise HTTPException(status_code=400, detail="Shot has no first frame or file is missing")
    # pick_first_frame already validated object_store existence for every
    # branch it can return — no extra local .exists() check needed (and it
    # would be wrong: src is a Path wrapping a COS key, not a local file).
    src_key = str(src)

    dest_key = f"{shot_custom_frames_prefix(project_id, shot_id)}{ts_uuid_name(Path(src_key).suffix or '.png')}"
    await object_store.copy(src_key, dest_key)

    shot.custom_first_frame_path = dest_key
    await session.commit()
    return {"shot_id": shot_id, "custom_first_frame_path": to_media_url(dest_key)}


@router.post("/projects/{project_id}/shots/{shot_id}/use-prev-last-frame")
async def use_prev_last_frame(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Copy the PREVIOUS shot's current last frame into this shot's custom_first_frame_path.

    The previous shot's last_frame_path already reflects any trim (trim re-extracts
    it), so this picks up the trimmed tail. Stored as a stable per-shot copy under
    custom_frames/ — a genuine user override that survives the previous shot's
    regeneration and is preserved by the auto-continuity logic.
    """
    await _get_project_or_404(project_id, session)
    if shot_id <= 1:
        raise HTTPException(status_code=400, detail="No previous shot")
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    from app.services.storage import shot_custom_frames_prefix

    prev_result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id - 1)
    )
    prev = prev_result.scalar_one_or_none()
    src_key = prev.last_frame_path if prev else None
    if not src_key or not await object_store.exists(src_key):
        raise HTTPException(status_code=400, detail="Previous shot has no last frame")

    dest_key = f"{shot_custom_frames_prefix(project_id, shot_id)}{ts_uuid_name(Path(src_key).suffix or '.png')}"
    await object_store.copy(src_key, dest_key)

    shot.custom_first_frame_path = dest_key
    await session.commit()
    return {"shot_id": shot_id, "custom_first_frame_path": to_media_url(dest_key)}


@router.post("/projects/{project_id}/shots/{shot_id}/extract-last-frame")
async def extract_last_frame(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Copy the shot's extracted last frame into target_last_frame_path (ts_uuid filename, tf_status=done)."""
    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    src_key = shot.last_frame_path
    if not src_key or not await object_store.exists(src_key):
        raise HTTPException(status_code=400, detail="Shot has no last frame or file is missing")

    dest_key = shot_key(project_id, shot_id, ts_uuid_name(Path(src_key).suffix or ".png"))
    await object_store.copy(src_key, dest_key)

    shot.target_last_frame_path = dest_key
    shot.tf_status = "done"
    await session.commit()
    return {
        "shot_id": shot_id,
        "target_last_frame_path": to_media_url(dest_key),
        "tf_status": "done",
    }


@router.delete("/projects/{project_id}/shots/{shot_id}/first-frame")
async def delete_first_frame(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Delete a shot's custom first frame, clearing the config and unlinking the file.

    Captures the DB-stored path before clearing, then removes the physical file
    (covers both uploaded ts_uuid filenames and other paths).
    Path-as-truth: whether a custom first frame is used is decided by checking
    custom_first_frame_path. Does NOT touch other fields.
    """
    await _get_project_or_404(project_id, session)

    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    # Prevent deleting while a first frame is being actively generated
    if shot.ff_status == "generating":
        raise HTTPException(
            status_code=409,
            detail="First frame is currently being generated; wait for it to complete",
        )

    # Capture the stored path BEFORE clearing — needed for unlink below
    old_key = shot.custom_first_frame_path

    # Clear the custom first frame path
    shot.custom_first_frame_path = None
    shot.ff_status = None
    shot.ff_error_message = None
    session.add(shot)
    await session.commit()

    # Remove the COS object at the DB-stored key (covers ts_uuid filenames
    # from uploaded frames)
    if old_key:
        await object_store.delete(old_key)

    return {
        "shot_id": shot_id,
        "custom_first_frame_path": None,
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


async def _fetch_dialog_source(ws, shot: Shot) -> Path:
    """Fetch the shot's source video into a workspace for ffprobe/ffmpeg.

    shot.video_path is the immutable source key — /trim, /restore-trim and
    /align-tail-frame only ever write trim_frames metadata, never overwrite
    it (see their docstrings: "source in COS is never touched"), and VC only
    ever sets vc_audio_path. So unlike the pre-Task-8 local-storage model
    (where trimming/VC physically rewrote the file), there is no separate
    "pristine vs derived" file to resolve here — the source is always just
    shot.video_path.
    """
    return await ws.fetch(shot.video_path, name="source.mp4")


@router.get("/projects/{project_id}/shots/{shot_id}/video-info")
async def get_shot_video_info(
    project_id: str,
    shot_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Return video metadata (fps, total_frames, duration) via ffprobe."""
    from app.agents.video_trimmer import get_video_info, speech_end_info

    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot or not shot.video_path:
        raise HTTPException(status_code=404, detail="Shot or video not found")

    async with workspace() as ws:
        local_source = await _fetch_dialog_source(ws, shot)
        source = str(local_source)
        info = get_video_info(source)
        # 容器时长可能含超出视频的音频尾巴（老生成产物）——时间轴统一按视频流计
        if info.get("fps"):
            info["duration"] = round(info["total_frames"] / info["fps"], 3)
        try:
            sec, frame = speech_end_info(source, info["fps"])
        except Exception:  # 静音检测失败不应阻塞裁剪元数据返回
            sec, frame = None, None
    # Restore is possible whenever a trim is currently applied — trim is
    # metadata-only (the source object is never overwritten, see
    # _fetch_dialog_source's docstring), so "has a backup" reduces to
    # "trim_frames is set" (path-as-truth, mirrors tf/vc/cc conventions).
    info["has_backup"] = shot.trim_frames is not None
    info["speech_end_sec"] = sec
    info["speech_end_frame"] = frame
    info["source_video_url"] = to_media_url(shot.video_path)
    return info


@router.get("/projects/{project_id}/shots/{shot_id}/waveform")
async def get_shot_waveform(
    project_id: str,
    shot_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Return audio waveform peaks for the shot video as a list of floats in [0,1]."""
    from app.agents.video_trimmer import extract_waveform_peaks, get_video_info

    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot or not shot.video_path:
        raise HTTPException(status_code=404, detail="Shot or video not found")
    try:
        async with workspace() as ws:
            local_source = await _fetch_dialog_source(ws, shot)
            source = str(local_source)
            info = get_video_info(source)
            video_seconds = info["total_frames"] / info["fps"] if info.get("fps") else None
            peaks = extract_waveform_peaks(source, max_seconds=video_seconds)
    except Exception:
        peaks = []
    return {"peaks": peaks}


@router.get("/projects/{project_id}/shots/{shot_id}/filmstrip")
async def get_shot_filmstrip(
    project_id: str,
    shot_id: int,
    count: int = 12,
    session: AsyncSession = Depends(get_session),
):
    """Return a horizontal thumbnail sprite URL for the shot's source video.

    Cache key is deterministic (hash of the source key + requested count) and
    lives in COS under the shot's own prefix, so re-opening the trim dialog on
    the same shot reuses the cached sprite and skips the ffmpeg tile pass
    entirely — otherwise every dialog-open would run ffmpeg again and leave a
    fresh object behind (accumulating one orphan per open, forever). Any other
    stale filmstrip_*.png under the shot prefix (e.g. from a previously
    requested `count`) is cleaned up on each call.
    """
    from app.agents.video_trimmer import extract_filmstrip_sprite, get_video_info

    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot or not shot.video_path:
        raise HTTPException(status_code=404, detail="Shot or video not found")
    n = max(4, min(count, 24))

    digest = hashlib.md5(shot.video_path.encode()).hexdigest()[:12]
    fname = f"filmstrip_{digest}_{n}.png"
    out_key = shot_key(project_id, shot_id, fname)

    async with workspace() as ws:
        local_source = await _fetch_dialog_source(ws, shot)

        if await object_store.exists(out_key):
            # Cache hit: same source+count already sprited — skip the ffmpeg
            # tile pass. A cheap ffprobe recomputes the actual cell count in
            # case the source is shorter than `count` frames.
            try:
                info = get_video_info(str(local_source))
                actual = max(1, min(n, max(1, int(info["total_frames"]))))
            except Exception:
                actual = n
        else:
            local_out = ws.path(fname)
            try:
                actual = extract_filmstrip_sprite(str(local_source), str(local_out), count=n)
            except Exception:
                raise HTTPException(status_code=500, detail="filmstrip 生成失败")
            await ws.publish(local_out, out_key)

    # Clean up sprites for a DIFFERENT count so a re-request with a changed
    # count doesn't leave orphaned objects behind under the shot prefix.
    stale_keys = [
        k for k in await object_store.list_prefix(shot_prefix(project_id, shot_id))
        if k.rsplit("/", 1)[-1].startswith("filmstrip_") and k != out_key
    ]
    for k in stale_keys:
        await object_store.delete(k)

    return {"url": to_media_url(out_key), "count": actual, "cell_aspect": 16 / 9}


async def _repoint_next_first_frame(
    project_id: str, shot_id: int, last_frame_path: str, session: AsyncSession
) -> tuple[int, str] | None:
    """Point the NEXT shot's auto first-frame at last_frame_path (preserve user overrides).

    Mirrors app.services.first_frame.propagate_first_frame_to_next: re-point when the next shot's
    custom_first_frame_path is empty or itself an auto-propagated last frame; never
    clobber a genuine user override stored under custom_frames/. Only touches the next
    shot while it is still un-generated.

    Returns (next_shot_id, last_frame_path) when it actually repointed, else None — so
    callers (e.g. trim) can surface the change to the frontend without a full refetch.
    """
    res = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id + 1)
    )
    nxt = res.scalar_one_or_none()
    if nxt is None or not nxt.use_prev_last_frame:
        return None
    # Only auto-adjust the next shot while it is still un-generated; never touch
    # the first frame of an already-rendered shot.
    if nxt.video_path:
        return None
    existing = nxt.custom_first_frame_path
    is_user_override = bool(existing) and "custom_frames" in existing
    if not is_user_override and existing != last_frame_path:
        nxt.custom_first_frame_path = last_frame_path
        session.add(nxt)
        return (nxt.shot_id, last_frame_path)
    return None


async def _reset_cc_and_clear_pre_cc_backup(shot: Shot) -> None:
    """Last frame changed → reset CC; clear the pre-CC backup key + object.

    素材变更审计（CLAUDE.md）：顺序固定为「先改 DB 解除引用，再删 COS 对象」——
    反过来在删除失败时会留下悬空引用（DB 指向一个可能已被删掉的对象）。
    This only mutates the passed-in ORM object + issues the COS delete; the
    caller is responsible for committing the session first.
    """
    shot.cc_status = None
    shot.cc_error_message = None
    stale_pre_cc = shot.pre_cc_last_frame_key
    shot.pre_cc_last_frame_key = None
    return stale_pre_cc


async def _publish_new_last_frame(
    ws, local_source: Path, frame_idx: int, project_id: str, shot_id: int
) -> str:
    """Extract *frame_idx* from the (already-fetched) local source video and
    publish it to a fresh, uniquely-named COS key. Returns the new key."""
    from app.agents.frame_porter import extract_frame_at

    local_lf = ws.path("new_last_frame.png")
    extract_frame_at(str(local_source), frame_idx, str(local_lf))
    return await ws.publish(
        local_lf, shot_key(project_id, shot_id, f"last_frame_{ts_uuid_name('.png')}")
    )


@router.post("/projects/{project_id}/shots/{shot_id}/trim")
async def trim_shot_video(
    project_id: str,
    shot_id: int,
    body: ShotTrimRequest,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Non-destructive trim: record trim_frames and refresh the last frame.

    shot.video_path (the source video object in COS) is never modified — only
    trim_frames metadata changes. Trimming changes the effective last frame
    (index N-1 of the source) → re-extract it, publish it to a fresh key, and
    reset CC. VC is untouched (the vc audio is full-length and independent of
    trim length).
    """
    from app.agents.video_trimmer import get_video_info

    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot or not shot.video_path:
        raise HTTPException(status_code=404, detail="Shot or video not found")
    if shot.status != "completed":
        raise HTTPException(status_code=409, detail="Shot is not completed")
    if body.end_frame < 24:
        raise HTTPException(status_code=400, detail="Must keep at least 24 frames")

    source_key = shot.video_path

    async with workspace() as ws:
        local_source = await ws.fetch(source_key, name="source.mp4")
        info = get_video_info(str(local_source))
        total = info["total_frames"]
        n = min(body.end_frame, total)  # clamp; full length is a no-op trim

        frame_idx = (n - 1) if n < total else (total - 1)
        new_lf_key = await _publish_new_last_frame(
            ws, local_source, frame_idx, project_id, shot_id
        )

    # 1. Metadata only — source object in COS is never touched
    shot.trim_frames = n if n < total else None
    shot.source_fps = info["fps"]
    shot.source_frames = total

    # 2. Point at the freshly-published last frame
    shot.last_frame_path = new_lf_key
    repointed = await _repoint_next_first_frame(project_id, shot.shot_id, new_lf_key, session)

    # 3. Last frame changed → reset CC + clear pre-CC backup (DB first, then COS delete)
    stale_pre_cc = await _reset_cc_and_clear_pre_cc_backup(shot)

    ts = int(datetime.utcnow().timestamp())
    await session.commit()
    if stale_pre_cc:
        await object_store.delete(stale_pre_cc)

    resp = {
        "video_path": to_media_url(shot.video_path),
        "last_frame_path": to_media_url(shot.last_frame_path),
        "trim_frames": shot.trim_frames,
        "trim_end_sec": (shot.trim_frames / info["fps"]) if shot.trim_frames else None,
        "version": ts,
        **info,
    }
    # If the next (un-generated) shot's first frame was auto-repointed to the new
    # trimmed last frame, surface it so the UI updates without a full refetch.
    if repointed:
        resp["next_shot"] = {
            "shot_id": repointed[0],
            "custom_first_frame_path": to_media_url(repointed[1]),
        }
    return resp


@router.post("/projects/{project_id}/shots/{shot_id}/restore-trim")
async def restore_trim(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Clear the trim: trim_frames=None, refresh last frame to the source's final frame."""
    from app.agents.video_trimmer import get_video_info

    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot or not shot.video_path:
        raise HTTPException(status_code=404, detail="Shot or video not found")

    source_key = shot.video_path

    async with workspace() as ws:
        local_source = await ws.fetch(source_key, name="source.mp4")
        info = get_video_info(str(local_source))
        total = info["total_frames"]
        new_lf_key = await _publish_new_last_frame(
            ws, local_source, total - 1, project_id, shot_id
        )

    shot.trim_frames = None
    shot.source_fps = info["fps"]
    shot.source_frames = total
    shot.last_frame_path = new_lf_key
    await _repoint_next_first_frame(project_id, shot.shot_id, new_lf_key, session)

    stale_pre_cc = await _reset_cc_and_clear_pre_cc_backup(shot)

    ts = int(datetime.utcnow().timestamp())
    await session.commit()
    if stale_pre_cc:
        await object_store.delete(stale_pre_cc)

    return {
        "video_path": to_media_url(shot.video_path),
        "last_frame_path": to_media_url(shot.last_frame_path),
        "trim_frames": None,
        "trim_end_sec": None,
        "version": ts,
        **info,
    }


@router.post("/projects/{project_id}/shots/{shot_id}/align-tail-frame")
async def align_tail_frame(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Non-destructive auto-trim: update trim_frames metadata to the frame that best
    matches the target tail frame (SSIM). shot.video_path (source) is never modified."""
    from app.agents.video_trimmer import find_best_tail_frame, get_video_info

    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot or not shot.video_path:
        raise HTTPException(status_code=404, detail="Shot or video not found")
    if not shot.target_last_frame_path:
        raise HTTPException(status_code=400, detail="No target tail frame for this shot")

    source_key = shot.video_path
    target_key = shot.target_last_frame_path

    async with workspace() as ws:
        local_source = await ws.fetch(source_key, name="source.mp4")
        local_target = await ws.fetch(target_key, name="target_last_frame.png")
        info = get_video_info(str(local_source))
        total = info["total_frames"]

        best = find_best_tail_frame(str(local_source), str(local_target))
        n = total if best is None else min(best, total)

        frame_idx = (n - 1) if n < total else (total - 1)
        new_lf_key = await _publish_new_last_frame(
            ws, local_source, frame_idx, project_id, shot_id
        )

    # 1. Metadata only — source object in COS is never touched
    shot.trim_frames = n if n < total else None
    shot.source_fps = info["fps"]
    shot.source_frames = total

    # 2. Point at the freshly-published last frame
    shot.last_frame_path = new_lf_key
    await _repoint_next_first_frame(project_id, shot.shot_id, new_lf_key, session)

    # 3. Last frame changed → reset CC. VC is untouched (consistent with /trim).
    stale_pre_cc = await _reset_cc_and_clear_pre_cc_backup(shot)

    ts = int(datetime.utcnow().timestamp())
    await session.commit()
    if stale_pre_cc:
        await object_store.delete(stale_pre_cc)

    return {
        "video_path": to_media_url(shot.video_path),
        "last_frame_path": to_media_url(shot.last_frame_path),
        "trim_frames": shot.trim_frames,
        "trim_end_sec": (shot.trim_frames / info["fps"]) if shot.trim_frames else None,
        "version": ts,
        "aligned_to_frame": n,
        **info,
    }


@router.post("/projects/{project_id}/shots/{shot_id}/detect-silence")
async def detect_silence(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Suggest a tail-trim point from trailing silence — read-only, no file writes.

    Returns a suggested end frame for the frontend to preview; the actual trim
    is performed later by the existing ``/trim`` endpoint when the user confirms.
    """
    from app.agents.video_trimmer import suggest_silence_trim, get_video_info

    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot or not shot.video_path:
        raise HTTPException(status_code=404, detail="Shot or video not found")

    async with workspace() as ws:
        local_source = await _fetch_dialog_source(ws, shot)
        source = str(local_source)
        suggestion = suggest_silence_trim(source)
        no_silence_info = None if suggestion is not None else get_video_info(source)
    if suggestion is None:
        return {
            "has_silence": False,
            "suggested_end_frame": None,
            "silence_start_time": None,
            **no_silence_info,
        }
    return {"has_silence": True, **suggestion}


@router.post("/projects/{project_id}/shots/{shot_id}/detect-speech-start")
async def detect_speech_start_ep(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """从开头静音推断语音起始帧 — 只读，不写文件。"""
    from app.agents.video_trimmer import detect_speech_start, get_video_info

    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot or not shot.video_path:
        raise HTTPException(status_code=404, detail="Shot or video not found")

    async with workspace() as ws:
        local_source = await _fetch_dialog_source(ws, shot)
        source = str(local_source)
        info = get_video_info(source)
        start_sec = detect_speech_start(source)
    if start_sec is None:
        return {"has_lead_silence": False, "suggested_start_frame": None, **info,
                "source_video_url": to_media_url(shot.video_path)}
    return {"has_lead_silence": True,
            "suggested_start_frame": int(round(start_sec * info["fps"])),
            "speech_start_sec": start_sec, **info,
            "source_video_url": to_media_url(shot.video_path)}


@router.put("/projects/{project_id}/shots/{shot_id}/audio-head-mute")
async def set_audio_head_mute(
    project_id: str,
    shot_id: int,
    body: dict,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """写前段静音帧数（0 = 清除）。纯 EDL，不动素材/trim/vc。"""
    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")
    n = int(body.get("head_mute_frames") or 0)
    shot.audio_head_mute_frames = n if n > 0 else None
    session.add(shot)
    await session.commit()
    sec = (shot.audio_head_mute_frames / shot.source_fps) if (shot.audio_head_mute_frames and shot.source_fps) else None
    return {"shot_id": shot_id, "audio_head_mute_frames": shot.audio_head_mute_frames,
            "audio_head_mute_sec": sec}


# Voice cloning / 音色校准 routes moved to app/api/voice.py (see voice.router).


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

    # Revert by pointing last_frame_path back at the pristine (un-calibrated)
    # last_frame — pristine_last_frame_key is the single source of truth for
    # this (never a directory scan): CC adopt never touches it (see
    # adopt_candidate_to_last_frame in app/api/image_candidates.py).
    pristine_key = shot.pristine_last_frame_key
    stale_cc_key = None
    if pristine_key and shot.last_frame_path != pristine_key:
        old = shot.last_frame_path
        if old and Path(old).name.startswith("cc_"):
            stale_cc_key = old
        shot.last_frame_path = pristine_key
        await _repoint_next_first_frame(project_id, shot_id, pristine_key, session)

    shot.cc_status = None
    shot.cc_error_message = None
    session.add(shot)
    await session.commit()
    # 素材审计（CLAUDE.md）：先提交 DB 解除引用，再删 COS 上的旧校准帧对象。
    if stale_cc_key:
        await object_store.delete(stale_cc_key)

    ts = int(datetime.utcnow().timestamp())
    return {
        "shot_id": shot_id,
        "cc_status": None,
        "last_frame_path": to_media_url(shot.last_frame_path),
        "version": ts,
    }
