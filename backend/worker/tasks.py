"""arq worker tasks for video generation pipeline."""

import json
import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app import observability
from app.models.project import Project, Shot, ReferenceImage, ImageCandidate
from app.services.state_machine import (
    ProjectStatus,
    ShotStatus,
    transition_project_status,
    InvalidTransitionError,
)
from app.services.first_frame import (
    pick_first_frame,
    init_shot1_first_frame,
    propagate_first_frame_to_next,
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
    get_original_video_for_audio,
    pristine_last_frame_path,
    ts_uuid_name,
    shot_candidates_dir,
)
from app.services.events import publish_event
from app.agents.llm import GeminiProvider
from app.agents.screenwriter import run_screenwriter as run_screenwriter_agent
from app.agents.director import run_director as run_director_agent
from app.agents.video_generator import generate_video
from app.agents.frame_porter import extract_last_frame
from app.agents.merger import merge_shots, merge_shots_with_crossfade

logger = logging.getLogger(__name__)


def resolve_tail_frame(target_last_frame_path: str | None) -> str | None:
    """Tail frame is used iff its path is set and the file exists.

    Path presence is the single source of truth — tf_confirmed is
    intentionally NOT consulted.
    """
    if target_last_frame_path:
        p = Path(target_last_frame_path)
        if p.exists():
            return str(p)
    return None


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
            # Eagerly populate shot 1's first frame for frontend visibility.
            await init_shot1_first_frame(project_id, shot, session)

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
            # Reuse the existing director take (the expensive LLM call) when a
            # motion_prompt is already stored; otherwise run the director now.
            if shot.motion_prompt:
                motion_prompt = shot.motion_prompt
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
                    reference_image_paths=(
                        json.loads(shot.custom_reference_paths)
                        if shot.custom_reference_paths else None
                    ),
                )

                # Refresh shot from DB to pick up any reference images
                # uploaded after the worker loaded the shot list. This must come
                # BEFORE assigning motion_prompt: with autoflush=False the refresh
                # re-reads the row and would discard an unsaved motion_prompt,
                # leaving the completed shot with motion_prompt=NULL (which hides
                # the "运镜提示词" edit button in the UI).
                await session.refresh(shot)
                shot.motion_prompt = motion_prompt

            # SINGLE SOURCE OF TRUTH for the first frame: resolve it fresh every run
            # from the one stored field (custom_first_frame_path) plus continuity
            # (prev last frame → refs). There is no persisted "resolved" copy to go
            # stale, so a re-uploaded 首帧 is always honored.
            # (None = multi-image reference mode.)
            first_frame = await pick_first_frame(project_id, shot, session)

            # Resolve reference image paths for multi-image mode
            ref_paths: Optional[list[str]] = None
            if first_frame is None and shot.custom_reference_paths:
                import json as _json

                ref_paths = _json.loads(shot.custom_reference_paths)

            # Use previous shot's last frame as first_frame
            # Guard: do NOT override when the user has set a custom first frame.
            # custom_first_frame_path is authoritative (path-as-truth).
            if shot.use_prev_last_frame and shot.shot_id > 1 and not shot.custom_first_frame_path:
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

            # Resolve target tail frame for Veo last_frame param (path presence only)
            last_frame = resolve_tail_frame(shot.target_last_frame_path)

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
            # Name the generated video uniquely (output_<ts>_<uuid>.mp4) so a
            # regenerated video is always a new URL — the browser can never replay a
            # cached copy. A fresh generation is a clean slate: delete EVERY prior
            # video (output_/trimmed_/vc_ — all uniquely named, no fixed backups), so
            # no stale trim/VC predecessor can survive and be sourced later.
            for _old in s_dir.glob("*.mp4"):
                _old.unlink(missing_ok=True)
            video_out = s_dir / f"output_{ts_uuid_name('.mp4')}"

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
                    reference_image_paths=ref_paths,
                    aspect_ratio=project.aspect_ratio,
                    last_frame_path=last_frame,
                )
                video_out.write_bytes(video_bytes)
                shot.video_path = str(video_out)
                from app.agents.video_trimmer import get_video_info as _gvi
                _src_info = _gvi(str(video_out))
                shot.source_fps = _src_info["fps"]
                shot.source_frames = _src_info["total_frames"]
                shot.trim_frames = None
                shot.vc_audio_path = None
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
                resolved_tail = resolve_tail_frame(shot.target_last_frame_path)
                if resolved_tail:
                    # Align-and-trim to the target tail frame (SSIM).
                    trim_result = auto_trim_to_tail_frame(
                        str(video_out), resolved_tail,
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

            # Extract last frame to a UNIQUE name so its URL changes on every
            # (re)generation — a stable last_frame.png would be replayed from the
            # browser cache (stale poster + stale next-shot first frame). Delete ALL
            # prior last_frame*.png, including the pre-CC backup: a fresh generation
            # makes the old extracted frames AND calibrated (cc_) frames stale too.
            for _old in list(s_dir.glob("last_frame*.png")) + list(s_dir.glob("cc_*.png")):
                _old.unlink(missing_ok=True)
            last_frame_out = s_dir / f"last_frame_{ts_uuid_name('.png')}"
            extract_last_frame(str(video_out), str(last_frame_out))
            shot.last_frame_path = str(last_frame_out)
            # Eagerly propagate last frame to next shot's first frame for frontend visibility.
            await propagate_first_frame_to_next(
                project_id, shot, str(last_frame_out), session
            )

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

            # Auto voice-calibration hook (retroactive=(a): only future completions)
            try:
                from worker.auto_vc import maybe_enqueue_auto_vc
                await maybe_enqueue_auto_vc(redis, session, project_id, project, shot)
            except Exception as e:
                logger.warning("Auto VC enqueue failed for shot %s: %s", getattr(shot, "shot_id", "?"), e)

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


def _resolve_ff_context(shot: Shot) -> str | None:
    """first_frame/custom-first 的 context 帧：目标尾帧 → 实际尾帧 → 无。"""
    ctx = resolve_tail_frame(shot.target_last_frame_path)
    if ctx:
        return ctx
    if shot.last_frame_path and Path(shot.last_frame_path).exists():
        return shot.last_frame_path
    return None


@observability.traced_job("worker-image-candidate-run", tags=["image-candidate"])
async def run_image_candidate(
    ctx: Dict[str, Any], project_id: str, shot_id: int, candidate_id: str, actor: str
) -> None:
    """统一图片候选生成：模式 = slot + 有无 custom_prompt；不触碰 project 状态机。"""
    from app.services import image_generation as ig

    worker_ctx = WorkerContext(ctx)
    session_factory = worker_ctx.session_factory
    redis = worker_ctx.redis

    async with session_factory() as session:
        project = (await session.execute(
            select(Project).where(Project.id == project_id)
        )).scalar_one_or_none()
        shot = (await session.execute(
            select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
        )).scalar_one_or_none()
        cand = (await session.execute(
            select(ImageCandidate).where(ImageCandidate.id == candidate_id)
        )).scalar_one_or_none()
        if not project or not shot or not cand:
            logger.error("run_image_candidate: missing project/shot/candidate (%s/%s/%s)",
                         project_id, shot_id, candidate_id)
            return

        await publish_event(redis, project_id, {
            "type": "image_candidate_started",
            "data": {"shot_id": shot_id, "candidate_id": candidate_id, "slot": cand.slot},
        })

        success = False
        try:
            refs = json.loads(cand.ref_paths) if cand.ref_paths else {}
            if "character" in refs:
                char_refs = refs["character"]
            else:
                char_refs = await _get_character_ref_paths(project_id, session)
            obj_refs = refs.get("object") or None

            out_dir = shot_candidates_dir(project_id, shot_id)
            out_dir.mkdir(parents=True, exist_ok=True)
            out = str(out_dir / ts_uuid_name(".png"))

            if cand.slot == "cc":
                pristine = pristine_last_frame_path(project_id, shot_id)
                if pristine is None and shot.last_frame_path and Path(shot.last_frame_path).exists():
                    pristine = Path(shot.last_frame_path)
                if pristine is None:
                    raise ValueError("Shot has no last frame to calibrate")
                await ig.calibrate_face(char_refs, str(pristine), out)

            elif cand.custom_prompt:
                if cand.slot == "tail_frame":
                    ff = await pick_first_frame(project_id, shot, session)
                    context = str(ff) if ff else None
                else:
                    context = _resolve_ff_context(shot)
                await ig.generate_custom(
                    prompt=cand.custom_prompt,
                    output_path=out,
                    character_ref_paths=char_refs,
                    object_ref_paths=obj_refs,
                    context_frame_path=context,
                    aspect_ratio=project.aspect_ratio,
                )

            elif cand.slot == "tail_frame":
                first_frame = await pick_first_frame(project_id, shot, session)
                if shot.motion_prompt and not obj_refs:
                    motion_prompt = shot.motion_prompt
                else:
                    motion_prompt = await run_director_agent(
                        shot_id=shot.shot_id,
                        shot_type=shot.shot_type,
                        visual_description=shot.visual_description,
                        text=shot.text,
                        duration=shot.shot_duration,
                        llm_provider=get_provider(),
                        reference_image_paths=obj_refs,
                    )
                    shot.motion_prompt = motion_prompt
                    session.add(shot)
                    await session.commit()

                async def _on_cot(end_pose: str) -> None:
                    await publish_event(redis, project_id, {
                        "type": "tf_pose_analyzed",
                        "data": {"shot_id": shot_id, "end_pose": end_pose},
                    })

                await ig.generate_tail_frame(
                    character_ref_paths=char_refs,
                    first_frame_path=str(first_frame) if first_frame else None,
                    motion_prompt=motion_prompt,
                    output_path=out,
                    object_ref_paths=obj_refs,
                    aspect_ratio=project.aspect_ratio,
                    on_cot_complete=_on_cot,
                )

            else:  # first_frame auto
                await ig.generate_first_frame(
                    character_ref_paths=char_refs,
                    context_frame_path=_resolve_ff_context(shot),
                    visual_description=shot.visual_description,
                    shot_type=shot.shot_type,
                    output_path=out,
                    motion_prompt=shot.motion_prompt,
                    object_ref_paths=obj_refs,
                    aspect_ratio=project.aspect_ratio,
                )

            cand.file_path = out
            cand.status = "done"
            cand.error = None
            if cand.slot == "cc":
                shot.cc_status = None
                shot.cc_error_message = None
                session.add(shot)
            session.add(cand)
            await session.commit()
            success = True
            logger.info("Image candidate %s done (slot=%s shot=%d)", candidate_id, cand.slot, shot_id)

        except Exception as e:
            logger.error("Image candidate %s failed: %s", candidate_id, e, exc_info=True)
            cand.status = "failed"
            cand.error = str(e)
            if cand.slot == "cc":
                shot.cc_status = "failed"
                shot.cc_error_message = str(e)
                session.add(shot)
            session.add(cand)
            await session.commit()
            await publish_event(redis, project_id, {
                "type": "image_candidate_failed",
                "data": {
                    "shot_id": shot_id,
                    "candidate_id": candidate_id,
                    "slot": cand.slot,
                    "error_message": str(e),
                },
            })

        if success:
            # Publish OUTSIDE the failure-handling try/except: a publish error here
            # must never overwrite the already-committed "done" status with "failed".
            try:
                await publish_event(redis, project_id, {
                    "type": "image_candidate_completed",
                    "data": {
                        "shot_id": shot_id,
                        "candidate_id": candidate_id,
                        "slot": cand.slot,
                        "file_path": to_media_url(out),
                    },
                })
            except Exception:
                logger.error(
                    "Failed to publish image_candidate_completed for %s", candidate_id,
                    exc_info=True,
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

        if not shots:
            raise ValueError("No completed shots to merge")

        final_path = final_video_path(project_id)
        final_path.parent.mkdir(parents=True, exist_ok=True)

        import tempfile, shutil as _shutil
        from app.agents.effective_clip import effective_clip_paths
        tmp_dir = tempfile.mkdtemp(prefix=f"export_{project_id}_")
        try:
            shot_paths = effective_clip_paths(list(shots), tmp_dir)
            if not shot_paths:
                raise ValueError("No completed shots to merge")
            cf = crossfade_duration if crossfade_duration is not None else settings.crossfade_duration
            merge_shots_with_crossfade(shot_paths, str(final_path), crossfade_duration=cf)

            project.final_video_path = str(final_path)
            session.add(project)
            await transition_project_status(
                project, ProjectStatus.EXPORTED, "system:worker", session, redis
            )
            await publish_event(
                redis, project_id,
                {"type": "export_done",
                 "data": {"final_video_path": f"/api/projects/{project_id}/final.mp4"}},
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
                redis, project_id,
                {"type": "pipeline_failed", "data": {"error_message": str(e)}},
            )
        finally:
            _shutil.rmtree(tmp_dir, ignore_errors=True)


async def _do_voice_convert_one(
    session_factory,
    redis,
    project_id: str,
    shot_id: int,
    ref_audio_path: str,
) -> None:
    """Voice-convert a single shot using the given reference audio.

    Extracts original audio from the shot, calls CosyVoice VC service,
    and stores the result as a new uniquely-named wav file. The source
    video is never touched; only shot.vc_audio_path is updated (metadata-only).
    """
    from app.agents.audio_extractor import extract_audio_wav
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
            # 1. Extract full source audio (trim-independent)
            source_video = get_original_video_for_audio(project_id, shot_id)
            src_audio = str(shot_dir(project_id, shot_id) / f"audio_in_{ts_uuid_name('.wav')}")
            extract_audio_wav(str(source_video), src_audio)

            # 2. CosyVoice VC → a NEW uniquely-named wav (never overwrite)
            vc_audio = str(shot_dir(project_id, shot_id) / f"audio_vc_{ts_uuid_name('.wav')}")
            await voice_convert(src_audio, ref_audio_path, vc_audio)

            # 3. Metadata only: drop a prior vc audio, point at the new one.
            #    Source video is NOT touched; video_path stays the source.
            if shot.vc_audio_path:
                Path(shot.vc_audio_path).unlink(missing_ok=True)
            Path(src_audio).unlink(missing_ok=True)
            shot.vc_audio_path = vc_audio
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
                        "vc_audio_url": to_media_url(vc_audio),
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
    worker_ctx = WorkerContext(ctx)
    session_factory = worker_ctx.session_factory
    redis = worker_ctx.redis

    async with session_factory() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()
        if not project:
            logger.error("Project %s not found", project_id)
            return
        from app.services.reference_voice import resolve_reference_prompt_wav
        ref_audio = resolve_reference_prompt_wav(project_id, project)
        if ref_audio is None:
            logger.error("Project %s has no reference voice set", project_id)
            return

    await _do_voice_convert_one(session_factory, redis, project_id, shot_id, str(ref_audio))


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
    worker_ctx = WorkerContext(ctx)
    session_factory = worker_ctx.session_factory
    redis = worker_ctx.redis

    async with session_factory() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()
        if not project:
            logger.error("Project %s not found", project_id)
            return
        from app.services.reference_voice import resolve_reference_prompt_wav
        ref_audio_path = resolve_reference_prompt_wav(project_id, project)
        if ref_audio_path is None:
            logger.error("Project %s has no reference voice set", project_id)
            return
    ref_audio = str(ref_audio_path)

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
    """校准一个 shot 的 last frame → 产出 cc 候选（采纳后才替换，见 adopt 端点）。"""
    from app.services import image_generation as ig

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

        cand = ImageCandidate(
            project_id=project_id, shot_pk=shot.id, shot_id=shot_id,
            slot="cc", status="generating", prompt_source="auto",
            ref_paths=json.dumps({"character": ref_image_paths}),
        )
        session.add(cand)
        await session.commit()
        await session.refresh(cand)

        try:
            # 从未校准的 pristine 帧出发（绝不叠加已校准帧）
            pristine = pristine_last_frame_path(project_id, shot_id) or Path(shot.last_frame_path)
            out_dir = shot_candidates_dir(project_id, shot_id)
            out_dir.mkdir(parents=True, exist_ok=True)
            out = str(out_dir / ts_uuid_name(".png"))
            await ig.calibrate_face(ref_image_paths, str(pristine), out)

            cand.file_path = out
            cand.status = "done"
            shot.cc_status = None
            shot.cc_error_message = None
            session.add_all([cand, shot])
            await session.commit()

            await publish_event(
                redis, project_id,
                {
                    "type": "cc_candidate_ready",
                    "data": {
                        "shot_id": shot_id,
                        "candidate_id": cand.id,
                        "file_path": to_media_url(out),
                    },
                },
            )
            logger.info("CC candidate ready for shot %d", shot_id)

        except Exception as e:
            logger.error("Character calibration failed for shot %d: %s", shot_id, e)
            cand.status = "failed"
            cand.error = str(e)
            session.add(cand)
            await session.commit()
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
