"""arq worker tasks for video generation pipeline."""

import json
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app import observability
from app.models.project import Project, Shot, ReferenceImage
from app.services.state_machine import (
    ProjectStatus,
    ShotStatus,
    transition_project_status,
    InvalidTransitionError,
)
from app.services.storage import (
    storyboard_path,
    shot_dir,
    reference_images_dir,
    final_video_path,
    ensure_shot_dir,
    to_media_url,
    shot_audio_original_path,
    shot_audio_vc_path,
    shot_pre_vc_video_path,
    shot_pre_cc_last_frame_path,
    shot_target_last_frame_path,
    get_original_video_for_audio,
)
from app.services.events import publish_event
from app.agents.llm import GeminiProvider
from app.agents.screenwriter import run_screenwriter as run_screenwriter_agent
from app.agents.director import run_director as run_director_agent
from app.agents.video_generator import generate_video
from app.agents.frame_porter import extract_last_frame
from app.agents.merger import merge_shots, merge_shots_with_crossfade

logger = logging.getLogger(__name__)


class WorkerContext:
    """Helper to access context in tasks."""

    def __init__(self, ctx: Dict[str, Any]):
        self.ctx = ctx

    @property
    def session_factory(self) -> async_sessionmaker:
        return self.ctx["session_factory"]

    @property
    def redis(self):
        return self.ctx["redis"]


async def _mark_shot_failed(
    session: AsyncSession,
    redis,
    project_id: str,
    shot: Shot,
    exc: Exception,
    *,
    status_field: str,
    status_value: str,
    error_field: str,
    event_type: str,
    shot_id: int,
) -> None:
    """Persist a shot failure (status + message), commit, and publish the failed event.

    Shared by the per-shot job error paths (generation / voice-convert / calibrate),
    which differ only in the status/error column names and the event type. Callers
    keep their own logging and control flow (``raise`` vs continue).
    """
    setattr(shot, status_field, status_value)
    setattr(shot, error_field, str(exc))
    session.add(shot)
    await session.commit()
    await publish_event(
        redis,
        project_id,
        {"type": event_type, "data": {"shot_id": shot_id, "error_message": str(exc)}},
    )


def get_provider() -> GeminiProvider:
    """Create Gemini provider from settings."""
    return GeminiProvider(project=settings.gemini_project, location=settings.gemini_location)


def get_prompts_dir() -> Path:
    """Get prompts directory."""
    return Path(__file__).parent.parent / "prompts"


@observability.traced_job("worker-screenwriter-run", tags=["screenwriter"])
async def run_screenwriter(ctx: Dict[str, Any], project_id: str, actor: str) -> None:
    """
    Run screenwriter agent to generate storyboard.

    Args:
        ctx: arq context with session_factory and redis
        project_id: Project ID
        actor: Who triggered this (e.g., 'user:alice')
    """
    worker_ctx = WorkerContext(ctx)
    session_factory = worker_ctx.session_factory
    redis = worker_ctx.redis

    async with session_factory() as session:
        # Get project
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()

        if not project:
            logger.error(f"Project {project_id} not found")
            return

        # Load reference images
        ref_result = await session.execute(
            select(ReferenceImage)
            .where(ReferenceImage.project_id == project_id)
            .order_by(ReferenceImage.kind, ReferenceImage.order_index)
        )
        ref_images = ref_result.scalars().all()

        reference_images_data = []
        for img in ref_images:
            path = Path(img.storage_path)
            if path.exists():
                reference_images_data.append(
                    {
                        "kind": img.kind,
                        "path": str(path),
                        "filename": img.filename,
                    }
                )

        # Run screenwriter
        provider = get_provider()

        try:
            storyboard_result = await run_screenwriter_agent(
                theme_text=project.theme_text,
                reference_images=reference_images_data,
                llm_provider=provider,
                aspect_ratio=project.aspect_ratio,
            )
        except Exception as e:
            logger.error(f"Screenwriter failed: {e}")
            project.error_message = str(e)
            session.add(project)
            await transition_project_status(
                project, ProjectStatus.FAILED, "system:worker", session, redis
            )
            return

        # Write storyboard.json
        sb_path = storyboard_path(project_id)
        sb_path.parent.mkdir(parents=True, exist_ok=True)
        sb_path.write_text(
            json.dumps(
                {
                    "scene_overview": storyboard_result["storyboard"]["scene_overview"],
                    "shots": storyboard_result["storyboard"]["shots"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        # Update project
        project.scene_overview = storyboard_result["storyboard"]["scene_overview"]
        project.storyboard_path = str(sb_path)
        session.add(project)

        # Create shots
        for shot_data in storyboard_result["storyboard"]["shots"]:
            shot = Shot(
                project_id=project_id,
                shot_id=shot_data["shot_id"],
                text=shot_data["text"],
                shot_type=shot_data["shot_type"],
                visual_description=shot_data["visual_description"],
                shot_duration=shot_data["shot_duration"],
                align_with_previous=shot_data.get("align_with_previous", True),
                word_count_warning=shot_data.get("word_count_warning", False),
                reference_image_hint=shot_data.get("reference_image_hint"),
            )
            session.add(shot)

        # Transition to SCRIPT_REVIEW
        await transition_project_status(
            project, ProjectStatus.SCRIPT_REVIEW, "system:worker", session, redis
        )

        # Publish event
        await publish_event(
            redis,
            project_id,
            {
                "type": "script_ready",
                "data": {
                    "storyboard": {
                        "scene_overview": storyboard_result["storyboard"][
                            "scene_overview"
                        ],
                        "shots": storyboard_result["storyboard"]["shots"],
                    },
                },
            },
        )

        logger.info(f"Screenwriter completed for project {project_id}")


@observability.traced_job("worker-shot-pipeline-run", tags=["shot-pipeline"])
async def run_shot_pipeline(
    ctx: Dict[str, Any], project_id: str, actor: str, shot_id: int | None = None,
) -> None:
    """
    Run shot pipeline: director + video generation for ONE pending shot.

    Processes only the first pending/failed shot (or a specific shot when
    *shot_id* is given), then transitions back to SHOT_REVIEW so the user
    can review before the next shot is generated.

    Args:
        ctx: arq context
        project_id: Project ID
        actor: Who triggered this
        shot_id: Optional — when given, process this specific shot instead of
                 the first pending one (used by confirm-tail-frame).
    """
    worker_ctx = WorkerContext(ctx)
    session_factory = worker_ctx.session_factory
    redis = worker_ctx.redis

    async with session_factory() as session:
        # Get project
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()

        if not project:
            logger.error(f"Project {project_id} not found")
            return

        if shot_id is not None:
            # Process a specific shot (e.g. after confirm-tail-frame)
            shot_result = await session.execute(
                select(Shot).where(
                    Shot.project_id == project_id, Shot.shot_id == shot_id
                )
            )
            shot = shot_result.scalar_one_or_none()
            if not shot:
                logger.error("Shot %d not found in project %s", shot_id, project_id)
                await transition_project_status(
                    project, ProjectStatus.SHOT_REVIEW, "system:worker", session, redis
                )
                return
        else:
            # Get pending shots
            shots_result = await session.execute(
                select(Shot)
                .where(
                    Shot.project_id == project_id,
                    Shot.status.in_([ShotStatus.PENDING.value, ShotStatus.FAILED.value]),
                )
                .order_by(Shot.shot_id)
            )
            pending_shots = shots_result.scalars().all()

            if not pending_shots:
                logger.info(f"No pending shots for project {project_id}")
                await transition_project_status(
                    project, ProjectStatus.SHOT_REVIEW, "system:worker", session, redis
                )
                return

            shot = pending_shots[0]

        provider = get_provider()
        genai_client = getattr(provider, "client", None)
        has_failures = False

        await publish_event(
            redis,
            project_id,
            {
                "type": "shot_started",
                "data": {"shot_id": shot.shot_id},
            },
        )

        try:
            # If tail frame was confirmed, reuse existing motion_prompt and first_frame
            if shot.tf_confirmed and shot.motion_prompt and shot.first_frame_path:
                motion_prompt = shot.motion_prompt
                first_frame = Path(shot.first_frame_path) if shot.first_frame_path else None
            else:
                # Run director
                shot.status = ShotStatus.PROMPT_GENERATING.value
                session.add(shot)
                await session.commit()

                motion_prompt = await run_director_agent(
                    shot_id=shot.shot_id,
                    shot_type=shot.shot_type,
                    visual_description=shot.visual_description,
                    text=shot.text,
                    duration=shot.shot_duration,
                    llm_provider=provider,
                )

                # Refresh shot from DB to pick up any reference images
                # uploaded after the worker loaded the shot list. This must come
                # BEFORE assigning motion_prompt: with autoflush=False the refresh
                # re-reads the row and would discard an unsaved motion_prompt,
                # leaving the completed shot with motion_prompt=NULL (which hides
                # the "运镜提示词" edit button in the UI).
                await session.refresh(shot)
                shot.motion_prompt = motion_prompt

                # Pick first frame (None = multi-image reference mode)
                first_frame = await _pick_first_frame(project_id, shot, session)
                shot.first_frame_path = str(first_frame) if first_frame else None

            # Resolve reference image paths for multi-image mode
            ref_paths: Optional[list[str]] = None
            if first_frame is None and shot.custom_reference_paths:
                import json as _json

                ref_paths = _json.loads(shot.custom_reference_paths)

            # Use previous shot's last frame as first_frame
            if shot.use_prev_last_frame and shot.shot_id > 1:
                prev_result = await session.execute(
                    select(Shot).where(
                        Shot.project_id == project_id,
                        Shot.shot_id == shot.shot_id - 1,
                    )
                )
                prev_shot = prev_result.scalar_one_or_none()
                if prev_shot and prev_shot.last_frame_path:
                    first_frame = Path(prev_shot.last_frame_path)
                    ref_paths = None

            # Resolve target tail frame for Veo last_frame param
            last_frame = None
            if shot.tf_confirmed and shot.target_last_frame_path:
                tf_path = Path(shot.target_last_frame_path)
                if tf_path.exists():
                    last_frame = str(tf_path)

            # Generate video
            shot.status = ShotStatus.VIDEO_GENERATING.value
            session.add(shot)
            await session.commit()

            await publish_event(
                redis,
                project_id,
                {
                    "type": "shot_progress",
                    "data": {"shot_id": shot.shot_id, "sub_status": "video_generating"},
                },
            )

            # Ensure shot directory
            ensure_shot_dir(project_id, shot.shot_id)
            s_dir = shot_dir(project_id, shot.shot_id)
            video_out = s_dir / "output.mp4"

            video_model = (
                settings.kie_veo_model
                if settings.video_provider == "kie"
                else settings.veo_model
            )
            with observability.generation(
                name="services-video-generate",
                model=f"{settings.video_provider}/{video_model}",
                input={
                    "motion_prompt": motion_prompt,
                    "first_frame_path": str(first_frame) if first_frame else None,
                    "last_frame_path": last_frame,
                    "reference_image_paths": ref_paths,
                    "shot_duration": shot.shot_duration,
                    "aspect_ratio": project.aspect_ratio,
                },
            ) as vid_gen:
                video_bytes = await generate_video(
                    client=genai_client,
                    motion_prompt=motion_prompt,
                    first_frame_path=str(first_frame) if first_frame else None,
                    shot_duration=shot.shot_duration,
                    spoken_text=shot.text,
                    reference_image_paths=ref_paths,
                    aspect_ratio=project.aspect_ratio,
                    last_frame_path=last_frame,
                )
                video_out.write_bytes(video_bytes)
                shot.video_path = str(video_out)
                observability.update_span(
                    vid_gen,
                    output={
                        "video_path": to_media_url(str(video_out)),
                        "size_bytes": len(video_bytes),
                    },
                )

            # Tail-frame alignment: auto-trim to the frame closest to the target
            if shot.auto_trim:
                from app.agents.video_trimmer import (
                    auto_trim_to_tail_frame,
                    auto_trim_to_speech_end,
                )
                if shot.tf_confirmed and shot.target_last_frame_path:
                    # Align-and-trim to the confirmed target tail frame (SSIM).
                    trim_result = auto_trim_to_tail_frame(
                        str(video_out), shot.target_last_frame_path,
                    )
                    trim_mode = "tail frame alignment"
                else:
                    # No tail-frame constraint: trim the trailing silence/frozen tail.
                    trim_result = auto_trim_to_speech_end(str(video_out))
                    trim_mode = "speech-end"
                if trim_result:
                    logger.info(
                        "Auto-trimmed shot %d to %d frames (%s)",
                        shot.shot_id, trim_result["trimmed_to_frame"], trim_mode,
                    )

            # Extract last frame
            last_frame_out = s_dir / "last_frame.png"
            extract_last_frame(str(video_out), str(last_frame_out))
            shot.last_frame_path = str(last_frame_out)

            # Mark as completed
            shot.status = ShotStatus.COMPLETED.value
            session.add(shot)
            await session.commit()

            await publish_event(
                redis,
                project_id,
                {
                    "type": "shot_completed",
                    "data": {
                        "shot_id": shot.shot_id,
                        "video_path": to_media_url(str(video_out)),
                        "last_frame_path": to_media_url(str(last_frame_out)),
                    },
                },
            )

        except Exception as e:
            logger.error(f"Shot {shot.shot_id} failed: {e}")
            has_failures = True
            await _mark_shot_failed(
                session, redis, project_id, shot, e,
                status_field="status", status_value=ShotStatus.FAILED.value,
                error_field="error_message", event_type="shot_failed",
                shot_id=shot.shot_id,
            )

        # Count remaining pending/failed shots
        remaining_result = await session.execute(
            select(Shot).where(
                Shot.project_id == project_id,
                Shot.status.in_([ShotStatus.PENDING.value, ShotStatus.FAILED.value]),
            )
        )
        remaining = len(remaining_result.scalars().all())

        total_result = await session.execute(
            select(Shot).where(Shot.project_id == project_id)
        )
        total = len(total_result.scalars().all())
        completed_count = total - remaining

        # Transition to SHOT_REVIEW
        await transition_project_status(
            project, ProjectStatus.SHOT_REVIEW, "system:worker", session, redis
        )

        if remaining == 0:
            await publish_event(
                redis,
                project_id,
                {
                    "type": "all_shots_ready",
                    "data": {"has_failures": has_failures},
                },
            )
        else:
            await publish_event(
                redis,
                project_id,
                {
                    "type": "shot_review_ready",
                    "data": {
                        "completed": completed_count,
                        "total": total,
                        "has_failures": has_failures,
                    },
                },
            )

        logger.info(
            f"Shot pipeline completed for project {project_id} ({completed_count}/{total})"
        )


async def _pick_first_frame(
    project_id: str, shot: Shot, session: AsyncSession
) -> Optional[Path]:
    """
    Pick the first frame for a shot.

    Returns None when the shot has custom_reference_paths (multi-image mode).
    For disconnected shots with custom_first_frame_path: use that image.
    For disconnected shots (shot_id > 1) without custom images: raise ValueError.
    For shot 1 without custom images: fall back to project character reference.
    For connected shots: use previous shot's last frame.
    """
    # Multi-image reference mode → return None (caller uses reference_images)
    # Only for disconnected shots; connected shots use custom refs for tail frame generation
    if shot.custom_reference_paths and not shot.align_with_previous:
        return None

    # Single custom first frame
    if shot.custom_first_frame_path:
        custom = Path(shot.custom_first_frame_path)
        if custom.exists():
            return custom

    # Try to get previous shot's last frame (for both connected and disconnected shots)
    if shot.shot_id > 1:
        prev_result = await session.execute(
            select(Shot).where(
                Shot.project_id == project_id, Shot.shot_id == shot.shot_id - 1
            )
        )
        prev_shot = prev_result.scalar_one_or_none()
        if prev_shot and prev_shot.last_frame_path:
            prev_path = Path(prev_shot.last_frame_path)
            if prev_path.exists():
                return prev_path

    # Fallback to character reference
    return await _get_first_character_ref(project_id, session)


async def _get_first_character_ref(project_id: str, session: AsyncSession) -> Path:
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

    path = Path(ref.storage_path)
    if not path.exists():
        raise ValueError(f"Reference image not found: {path}")

    return path


async def _get_character_ref_paths(
    project_id: str, session: AsyncSession
) -> list[str]:
    """Get all character reference image paths for a project."""
    result = await session.execute(
        select(ReferenceImage).where(
            ReferenceImage.project_id == project_id,
            ReferenceImage.kind == "character",
        )
    )
    refs = result.scalars().all()
    return [r.storage_path for r in refs if Path(r.storage_path).exists()]


@observability.traced_job("worker-tail-frame-pipeline-run", tags=["tail-frame"])
async def run_tail_frame_pipeline(
    ctx: Dict[str, Any], project_id: str, shot_id: int, actor: str
) -> None:
    """Generate target tail frame for a shot: pick_first_frame + director + tail frame.

    After completion, transitions back to SHOT_REVIEW so the user can
    confirm the generated tail frame before video generation proceeds.

    Args:
        ctx: arq context
        project_id: Project ID
        shot_id: Shot sequence number
        actor: Who triggered this
    """
    from app.services.tail_frame_generator import generate_tail_frame

    worker_ctx = WorkerContext(ctx)
    session_factory = worker_ctx.session_factory
    redis = worker_ctx.redis

    async with session_factory() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()
        if not project:
            logger.error("Project %s not found", project_id)
            return

        result = await session.execute(
            select(Shot).where(
                Shot.project_id == project_id, Shot.shot_id == shot_id
            )
        )
        shot = result.scalar_one_or_none()
        if not shot:
            logger.error("Shot %d not found in project %s", shot_id, project_id)
            return

        provider = get_provider()

        await publish_event(
            redis, project_id,
            {"type": "tf_started", "data": {"shot_id": shot_id}},
        )

        try:
            # 1. Pick first frame
            first_frame = await _pick_first_frame(project_id, shot, session)
            shot.first_frame_path = str(first_frame) if first_frame else None

            # 2. Director → generate motion_prompt (skip if already exists)
            if shot.motion_prompt:
                motion_prompt = shot.motion_prompt
                logger.info("Reusing existing motion_prompt for shot %d", shot_id)
            else:
                shot.status = ShotStatus.PROMPT_GENERATING.value
                session.add(shot)
                await session.commit()

                motion_prompt = await run_director_agent(
                    shot_id=shot.shot_id,
                    shot_type=shot.shot_type,
                    visual_description=shot.visual_description,
                    text=shot.text,
                    duration=shot.shot_duration,
                    llm_provider=provider,
                )
                shot.motion_prompt = motion_prompt

            # 3. Generate target tail frame
            shot.tf_status = "generating"
            session.add(shot)
            await session.commit()

            char_refs = await _get_character_ref_paths(project_id, session)

            # Object reference images (from custom_reference_paths on connected shots)
            obj_refs = None
            if shot.custom_reference_paths:
                obj_refs = json.loads(shot.custom_reference_paths)

            ensure_shot_dir(project_id, shot.shot_id)
            tf_output = str(shot_target_last_frame_path(project_id, shot.shot_id))

            async def _on_cot_complete(end_pose: str) -> None:
                await publish_event(
                    redis, project_id,
                    {
                        "type": "tf_pose_analyzed",
                        "data": {"shot_id": shot_id, "end_pose": end_pose},
                    },
                )

            await generate_tail_frame(
                character_ref_paths=char_refs,
                first_frame_path=str(first_frame) if first_frame else None,
                motion_prompt=motion_prompt,
                output_path=tf_output,
                object_ref_paths=obj_refs,
                aspect_ratio=project.aspect_ratio,
                on_cot_complete=_on_cot_complete,
            )

            shot.target_last_frame_path = tf_output
            shot.tf_status = "done"
            shot.tf_error_message = None
            shot.tf_confirmed = False
            shot.status = ShotStatus.PENDING.value
            session.add(shot)
            await session.commit()

            await publish_event(
                redis, project_id,
                {
                    "type": "tf_completed",
                    "data": {
                        "shot_id": shot_id,
                        "target_last_frame_path": to_media_url(tf_output),
                        "motion_prompt": motion_prompt,
                    },
                },
            )
            logger.info("Tail frame generated for shot %d in project %s", shot_id, project_id)

        except Exception as e:
            logger.error("Tail frame pipeline failed for shot %d: %s", shot_id, e, exc_info=True)
            shot.tf_status = "failed"
            shot.tf_error_message = str(e)
            shot.status = ShotStatus.PENDING.value
            session.add(shot)
            await session.commit()

            await publish_event(
                redis, project_id,
                {
                    "type": "tf_failed",
                    "data": {"shot_id": shot_id, "error_message": str(e)},
                },
            )

        # Transition back to SHOT_REVIEW
        await transition_project_status(
            project, ProjectStatus.SHOT_REVIEW, "system:worker", session, redis
        )


async def run_merger(
    ctx: Dict[str, Any],
    project_id: str,
    actor: str,
    crossfade_duration: float | None = None,
) -> None:
    """
    Merge all completed shots into final video.

    Args:
        ctx: arq context
        project_id: Project ID
        actor: Who triggered this
        crossfade_duration: Override for crossfade (None = use settings default)
    """
    worker_ctx = WorkerContext(ctx)
    session_factory = worker_ctx.session_factory
    redis = worker_ctx.redis

    async with session_factory() as session:
        # Get project
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()

        if not project:
            logger.error(f"Project {project_id} not found")
            return

        # Get completed shots
        shots_result = await session.execute(
            select(Shot)
            .where(
                Shot.project_id == project_id, Shot.status == ShotStatus.COMPLETED.value
            )
            .order_by(Shot.shot_id)
        )
        shots = shots_result.scalars().all()

        shot_paths = [s.video_path for s in shots if s.video_path]

        if not shot_paths:
            raise ValueError("No completed shots to merge")

        final_path = final_video_path(project_id)
        final_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            cf = crossfade_duration if crossfade_duration is not None else settings.crossfade_duration
            merge_shots_with_crossfade(shot_paths, str(final_path), crossfade_duration=cf)

            project.final_video_path = str(final_path)
            session.add(project)

            await transition_project_status(
                project, ProjectStatus.EXPORTED, "system:worker", session, redis
            )

            await publish_event(
                redis,
                project_id,
                {
                    "type": "export_done",
                    "data": {
                        "final_video_path": f"/api/projects/{project_id}/final.mp4"
                    },
                },
            )

            logger.info(f"Merger completed for project {project_id}")

        except Exception as e:
            logger.error(f"Merger failed for project {project_id}: {e}")
            project.error_message = str(e)
            session.add(project)

            await transition_project_status(
                project, ProjectStatus.FAILED, "system:worker", session, redis
            )

            await publish_event(
                redis,
                project_id,
                {
                    "type": "pipeline_failed",
                    "data": {"error_message": str(e)},
                },
            )


async def _do_voice_convert_one(
    session_factory,
    redis,
    project_id: str,
    shot_id: int,
    ref_audio_path: str,
) -> None:
    """Voice-convert a single shot using the given reference audio.

    Extracts original audio from the shot, calls CosyVoice VC service,
    and remuxes the result back into the video.
    """
    from app.agents.audio_extractor import extract_audio_wav, remux_video_with_audio
    from app.services.cosyvoice_client import voice_convert

    async with session_factory() as session:
        result = await session.execute(
            select(Shot).where(
                Shot.project_id == project_id, Shot.shot_id == shot_id
            )
        )
        shot = result.scalar_one_or_none()
        if not shot or not shot.video_path:
            raise ValueError(f"Shot {shot_id} not found or has no video")

        await publish_event(
            redis, project_id,
            {"type": "vc_started", "data": {"shot_id": shot_id}},
        )

        try:
            # 1. Extract original audio (always from unmodified video)
            original_video = get_original_video_for_audio(project_id, shot_id)
            src_audio = str(shot_audio_original_path(project_id, shot_id))
            extract_audio_wav(str(original_video), src_audio)

            # 2. Call CosyVoice VC service
            vc_audio = str(shot_audio_vc_path(project_id, shot_id))
            await voice_convert(src_audio, ref_audio_path, vc_audio)

            # 3. Backup current video before VC (if not already backed up)
            pre_vc = shot_pre_vc_video_path(project_id, shot_id)
            video_path = Path(shot.video_path)
            if not pre_vc.exists():
                import shutil
                shutil.copy2(str(video_path), str(pre_vc))

            # 4. Remux: video stream from backup + converted audio → output.mp4
            remux_video_with_audio(str(pre_vc), vc_audio, str(video_path))

            # 5. Update DB
            shot.vc_status = "done"
            shot.vc_error_message = None
            session.add(shot)
            await session.commit()

            import time as _time
            await publish_event(
                redis, project_id,
                {
                    "type": "vc_completed",
                    "data": {
                        "shot_id": shot_id,
                        "video_path": to_media_url(str(video_path)),
                        "version": int(_time.time()),
                    },
                },
            )
            logger.info("Voice conversion completed for shot %d", shot_id)

        except Exception as e:
            logger.error("Voice conversion failed for shot %d: %s", shot_id, e)
            await _mark_shot_failed(
                session, redis, project_id, shot, e,
                status_field="vc_status", status_value="failed",
                error_field="vc_error_message", event_type="vc_failed",
                shot_id=shot_id,
            )
            raise


async def run_voice_convert(
    ctx: Dict[str, Any], project_id: str, shot_id: int, actor: str
) -> None:
    """Voice-convert a single shot to match the project's reference voice.

    Args:
        ctx: arq context with session_factory and redis
        project_id: Project ID
        shot_id: Shot ID to convert
        actor: Who triggered this
    """
    from app.agents.audio_extractor import extract_audio_wav

    worker_ctx = WorkerContext(ctx)
    session_factory = worker_ctx.session_factory
    redis = worker_ctx.redis

    async with session_factory() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()
        if not project or not project.reference_voice_shot_id:
            logger.error("Project %s not found or no reference voice set", project_id)
            return

        ref_shot_id = project.reference_voice_shot_id

    # Extract reference audio
    ref_audio = str(shot_audio_original_path(project_id, ref_shot_id))
    if not Path(ref_audio).exists():
        ref_video = get_original_video_for_audio(project_id, ref_shot_id)
        extract_audio_wav(str(ref_video), ref_audio)

    await _do_voice_convert_one(session_factory, redis, project_id, shot_id, ref_audio)


async def run_voice_convert_batch(
    ctx: Dict[str, Any], project_id: str, shot_ids: list[int], actor: str
) -> None:
    """Voice-convert multiple shots to match the project's reference voice.

    Args:
        ctx: arq context
        project_id: Project ID
        shot_ids: List of shot IDs to convert
        actor: Who triggered this
    """
    from app.agents.audio_extractor import extract_audio_wav

    worker_ctx = WorkerContext(ctx)
    session_factory = worker_ctx.session_factory
    redis = worker_ctx.redis

    async with session_factory() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()
        if not project or not project.reference_voice_shot_id:
            logger.error("Project %s not found or no reference voice set", project_id)
            return

        ref_shot_id = project.reference_voice_shot_id

    # Extract reference audio once
    ref_audio = str(shot_audio_original_path(project_id, ref_shot_id))
    if not Path(ref_audio).exists():
        ref_video = get_original_video_for_audio(project_id, ref_shot_id)
        extract_audio_wav(str(ref_video), ref_audio)

    converted = 0
    failed = 0
    for sid in shot_ids:
        try:
            await _do_voice_convert_one(session_factory, redis, project_id, sid, ref_audio)
            converted += 1
        except Exception:
            failed += 1

    await publish_event(
        redis, project_id,
        {
            "type": "vc_batch_done",
            "data": {"converted": converted, "failed": failed},
        },
    )
    logger.info(
        "Batch voice conversion for project %s: %d converted, %d failed",
        project_id, converted, failed,
    )


# ============== Character Calibration ==============


async def _do_character_calibrate_one(
    session_factory,
    redis,
    project_id: str,
    shot_id: int,
    ref_image_paths: list[str],
) -> None:
    """Calibrate face in a single shot's last frame using reference images."""
    from app.services.face_calibration_client import calibrate_face

    async with session_factory() as session:
        result = await session.execute(
            select(Shot).where(
                Shot.project_id == project_id, Shot.shot_id == shot_id
            )
        )
        shot = result.scalar_one_or_none()
        if not shot or not shot.last_frame_path:
            raise ValueError(f"Shot {shot_id} not found or has no last frame")

        await publish_event(
            redis, project_id,
            {"type": "cc_started", "data": {"shot_id": shot_id}},
        )

        try:
            import shutil
            last_frame = Path(shot.last_frame_path)
            pre_cc = shot_pre_cc_last_frame_path(project_id, shot_id)

            if pre_cc.exists():
                # Restore original frame before re-calibrating
                shutil.copy2(str(pre_cc), str(last_frame))
            else:
                # First time — backup the original
                shutil.copy2(str(last_frame), str(pre_cc))

            # Run face calibration (overwrites last_frame in place)
            await calibrate_face(ref_image_paths, str(last_frame), str(last_frame))

            # Update DB
            shot.cc_status = "done"
            shot.cc_error_message = None
            session.add(shot)
            await session.commit()

            await publish_event(
                redis, project_id,
                {
                    "type": "cc_completed",
                    "data": {
                        "shot_id": shot_id,
                        "last_frame_path": to_media_url(str(last_frame)),
                    },
                },
            )
            logger.info("Character calibration completed for shot %d", shot_id)

        except Exception as e:
            logger.error("Character calibration failed for shot %d: %s", shot_id, e)
            await _mark_shot_failed(
                session, redis, project_id, shot, e,
                status_field="cc_status", status_value="failed",
                error_field="cc_error_message", event_type="cc_failed",
                shot_id=shot_id,
            )
            raise


@observability.traced_job("worker-character-calibrate-run", tags=["character-calibrate"])
async def run_character_calibrate(
    ctx: Dict[str, Any], project_id: str, shot_id: int, actor: str
) -> None:
    """Character-calibrate a single shot's last frame."""
    worker_ctx = WorkerContext(ctx)
    session_factory = worker_ctx.session_factory
    redis = worker_ctx.redis

    async with session_factory() as session:
        result = await session.execute(
            select(ReferenceImage).where(
                ReferenceImage.project_id == project_id,
                ReferenceImage.kind == "character",
            )
        )
        refs = result.scalars().all()
        if not refs:
            logger.error("Project %s has no character reference images", project_id)
            return

    ref_paths = [r.storage_path for r in refs]
    await _do_character_calibrate_one(session_factory, redis, project_id, shot_id, ref_paths)


@observability.traced_job("worker-character-calibrate-batch-run", tags=["character-calibrate"])
async def run_character_calibrate_batch(
    ctx: Dict[str, Any], project_id: str, shot_ids: list[int], actor: str
) -> None:
    """Character-calibrate multiple shots' last frames."""
    worker_ctx = WorkerContext(ctx)
    session_factory = worker_ctx.session_factory
    redis = worker_ctx.redis

    async with session_factory() as session:
        result = await session.execute(
            select(ReferenceImage).where(
                ReferenceImage.project_id == project_id,
                ReferenceImage.kind == "character",
            )
        )
        refs = result.scalars().all()
        if not refs:
            logger.error("Project %s has no character reference images", project_id)
            return

    ref_paths = [r.storage_path for r in refs]

    calibrated = 0
    failed = 0
    for sid in shot_ids:
        try:
            await _do_character_calibrate_one(session_factory, redis, project_id, sid, ref_paths)
            calibrated += 1
        except Exception:
            failed += 1

    await publish_event(
        redis, project_id,
        {
            "type": "cc_batch_done",
            "data": {"calibrated": calibrated, "failed": failed},
        },
    )
    logger.info(
        "Batch character calibration for project %s: %d calibrated, %d failed",
        project_id, calibrated, failed,
    )
