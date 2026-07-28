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
    to_media_url,
    ts_uuid_name,
    shot_key,
)
from app.services import object_store
from app.services.workspace import workspace, ensure_free_space, Workspace
from app.services.storyboard import write_storyboard
from app.services.events import publish_event
from app.agents.llm import GeminiProvider
from app.agents.screenwriter import run_screenwriter as run_screenwriter_agent
from app.agents.director import run_director as run_director_agent
from app.agents.video_generator import generate_video
from app.agents.frame_porter import extract_last_frame
from app.agents.merger import merge_shots
from app.agents.effective_clip import ClipSpec, effective_clip_paths

# 注：以下名称曾从 app.services.storage 导入，Task 5 删除本地路径函数后不再
# 存在：shot_audio_original_path / shot_audio_vc_path / shot_pre_vc_video_path /
# shot_pre_cc_last_frame_path / get_original_video_for_audio /
# pristine_last_frame_path / shot_candidates_dir — 全部死 import，正文从未
# 使用，或已被后续 task 迁移掉。storyboard_path/shot_dir/reference_images_dir/
# final_video_path/ensure_shot_dir 曾同样是 NameError 陷阱，Task 11（导出/
# 删除/storyboard）已修复：run_screenwriter 的 storyboard 写入改为
# storyboard.write_storyboard；run_merger 改为调用下面的
# merge_project_shots（workspace + effective_clip + COS）。
#
# Task 9（VC 与 CC 链路）已修复：resolve_tail_frame/_resolve_ff_context/
# _get_character_ref_paths 的 Path(key).exists() 本地磁盘判断（对 COS key 恒
# 假，静默禁用 Veo 尾帧定向/参考图解析，无任何报错）改为 object_store.exists()；
# _do_voice_convert_one/_do_character_calibrate_one 全面改为 workspace()+COS
# key（pristine 尾帧读 shot.pristine_last_frame_key，不再目录扫描）。
# Task 10（Task 11b，紧急插入，此前无人认领）已修复：run_image_candidate 的
# shot_candidates_dir（候选图输出目录，first_frame/tail_frame/cc 三种 slot 共用
# 同一段代码）此前无条件在 slot 分支之前引用，NameError 必然命中——经
# app/api/image_candidates.py 的 create_image_candidate 端点可达，每次创建候选
# 图都会入队一个必崩的任务。现改为 workspace()+COS：全部参考图/上下文帧/
# pristine 帧先 fetch 进一次性 workspace 再传给 image_generation.py 的
# calibrate_face/generate_custom/generate_tail_frame/generate_first_frame（它们
# 只认本地文件路径），产物 publish 到 shot_candidates_prefix 下的新 key。

logger = logging.getLogger(__name__)


async def resolve_tail_frame(target_last_frame_path: str | None) -> str | None:
    """Tail frame is used iff its key is set and the object exists in COS.

    Path presence is the single source of truth — tf_confirmed is
    intentionally NOT consulted. target_last_frame_path is a COS key, not a
    local path: Path(key).exists() is always False for a real key, which
    used to silently disable Veo tail-frame targeting for every shot with a
    configured target — no error, ever. Existence must be checked against
    the object store.
    """
    if target_last_frame_path:
        if await object_store.exists(target_last_frame_path):
            return target_last_frame_path
    return None


async def publish_generated_video(
    session_factory: async_sessionmaker,
    project_id: str,
    shot_id: int,
    video_bytes: bytes,
) -> tuple[str, str]:
    """把生成的视频字节发布到 COS，抽取尾帧，更新 DB。

    返回 (video_key, last_frame_key)。

    一致性：video、last_frame 两个对象都 put 成功后才写 DB，保证 DB 中的
    key 永远指向真实存在的对象。同时写入 pristine_last_frame_key——角色校准
    (CC) 会直接覆盖 last_frame_path，这是唯一能追溯回校准前原始尾帧的字段。
    """
    async with workspace() as ws:
        local_video = ws.path(f"output_{ts_uuid_name('.mp4')}")
        local_video.write_bytes(video_bytes)

        local_frame = ws.path(f"last_frame_{ts_uuid_name('.png')}")
        extract_last_frame(str(local_video), str(local_frame))

        video_key = await ws.publish(
            local_video, shot_key(project_id, shot_id, local_video.name))
        frame_key = await ws.publish(
            local_frame, shot_key(project_id, shot_id, local_frame.name))

    async with session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
        )).scalar_one()
        shot.video_path = video_key
        shot.last_frame_path = frame_key
        # CC 会覆盖 last_frame_path，故同时记录未校准的原始尾帧作为还原目标
        shot.pristine_last_frame_key = frame_key
        await s.commit()

    return video_key, frame_key


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

        # img.storage_path is a COS key — Path(key).exists() is always False,
        # so it must be fetched into a local workspace before the LLM call
        # (see C2: this used to silently drop every reference image).
        async with workspace() as ref_ws:
            reference_images_data = []
            for i, img in enumerate(ref_images):
                if not await object_store.exists(img.storage_path):
                    continue
                local = await ref_ws.fetch(
                    img.storage_path,
                    name=f"ref_{i}{Path(img.storage_path).suffix or '.png'}",
                )
                reference_images_data.append(
                    {
                        "kind": img.kind,
                        "path": str(local),
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
        sb_key = await write_storyboard(
            project_id,
            storyboard_result["storyboard"]["scene_overview"],
            storyboard_result["storyboard"]["shots"],
        )

        # Update project
        project.scene_overview = storyboard_result["storyboard"]["scene_overview"]
        project.storyboard_path = sb_key
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

                # shot.custom_reference_paths holds COS keys — director.run_director
                # does Path(p).exists() (always False for a key), so they must be
                # fetched into a local workspace first (C2).
                obj_ref_keys = (
                    json.loads(shot.custom_reference_paths)
                    if shot.custom_reference_paths else None
                )
                async with workspace() as ref_ws:
                    local_obj_refs = await _fetch_existing_refs(
                        ref_ws, obj_ref_keys, "objref"
                    )
                    motion_prompt = await run_director_agent(
                        shot_id=shot.shot_id,
                        shot_type=shot.shot_type,
                        visual_description=shot.visual_description,
                        text=shot.text,
                        duration=shot.shot_duration,
                        llm_provider=provider,
                        reference_image_paths=local_obj_refs or None,
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

            # 自愈悬空的自动传播首帧指针：指向的对象已被清理（如上一镜裁剪时删除旧末帧）
            # 时，把指针改到本次实际解析出的首帧；用户上传/提取的 custom_frames/ 覆盖永不改动。
            # custom_first_frame_path 存的是 COS key，"存在" 要问 object_store 而非本地磁盘
            # ——否则任何合法 key 都会被误判为悬空，用角色参考图静默顶替上一镜尾帧。
            stale_auto_ptr = (
                shot.custom_first_frame_path
                and "custom_frames" not in shot.custom_first_frame_path
                and not await object_store.exists(shot.custom_first_frame_path)
            )
            if stale_auto_ptr:
                shot.custom_first_frame_path = str(first_frame) if first_frame else None
                session.add(shot)

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
            last_frame = await resolve_tail_frame(shot.target_last_frame_path)

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

            # Generated artifacts are published straight to COS (no local shot
            # directory to manage any more — see publish_generated_video). A
            # regenerated video/last-frame always gets a fresh ts_uuid_name(),
            # so its key/URL is always new — the browser can never replay a
            # cached copy. Prior output_/trimmed_/vc_ objects become orphans,
            # which the project's stated consistency rule explicitly accepts
            # ("宁可留孤儿对象") rather than risk deleting a key still referenced
            # elsewhere.
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

                # Tail-frame alignment / speech-end auto-trim mutate the video
                # in place and need a real local file (ffmpeg) — done on a
                # throwaway local copy BEFORE anything is published to COS, so
                # the object that lands in COS is already the final cut.
                async with workspace() as trim_ws:
                    local_video = trim_ws.path(f"gen_{ts_uuid_name('.mp4')}")
                    local_video.write_bytes(video_bytes)

                    if shot.auto_trim:
                        from app.agents.video_trimmer import (
                            auto_trim_to_tail_frame,
                            auto_trim_to_speech_end,
                        )
                        resolved_tail = await resolve_tail_frame(shot.target_last_frame_path)
                        if resolved_tail:
                            # Align-and-trim to the target tail frame (SSIM).
                            trim_result = auto_trim_to_tail_frame(
                                str(local_video), resolved_tail,
                            )
                            trim_mode = "tail frame alignment"
                        else:
                            # No tail-frame constraint: trim trailing silence/frozen tail.
                            trim_result = auto_trim_to_speech_end(str(local_video))
                            trim_mode = "speech-end"
                        if trim_result:
                            logger.info(
                                "Auto-trimmed shot %d to %d frames (%s)",
                                shot.shot_id, trim_result["trimmed_to_frame"], trim_mode,
                            )

                    from app.agents.video_trimmer import get_video_info as _gvi
                    _src_info = _gvi(str(local_video))
                    video_bytes = local_video.read_bytes()

                shot.source_fps = _src_info["fps"]
                shot.source_frames = _src_info["total_frames"]
                shot.trim_frames = None
                shot.vc_audio_path = None

                # Publish the (possibly trimmed) video + its extracted last
                # frame to COS and update the DB — both objects are put()
                # successfully before either key is written to the row.
                # publish_generated_video writes via its OWN session (so the
                # write is atomic and independent of this function's later
                # commits) — mirror the same values onto this outer `shot`
                # object so it isn't left stale: anything added after this
                # point that reads shot.video_path/last_frame_path must see
                # the real key, not None/whatever it was before generation.
                video_key, last_frame_key = await publish_generated_video(
                    session_factory, project_id, shot.shot_id, video_bytes,
                )
                shot.video_path = video_key
                shot.last_frame_path = last_frame_key
                shot.pristine_last_frame_key = last_frame_key

                observability.update_span(
                    vid_gen,
                    output={
                        "video_path": to_media_url(video_key),
                        "size_bytes": len(video_bytes),
                    },
                )

            # Eagerly propagate last frame to next shot's first frame for frontend visibility.
            await propagate_first_frame_to_next(
                project_id, shot, last_frame_key, session
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
                        "video_path": to_media_url(video_key),
                        "last_frame_path": to_media_url(last_frame_key),
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
    return [r.storage_path for r in refs if await object_store.exists(r.storage_path)]


async def _fetch_existing_refs(
    ws: Workspace, keys: Optional[list[str]], label: str
) -> list[str]:
    """Fetch each still-existing COS key into ``ws``, skip stale/missing ones.

    LLM providers (app.agents.llm._build_contents / director.run_director) only
    accept real filesystem paths — they do a bare ``Path(p).exists()`` check
    that is unconditionally False for a COS key, so passing keys straight
    through silently drops every reference image with no error and no log
    (see the C2 finding this fixes). Mirrors _get_character_ref_paths'
    existence filtering but also materializes local files, since the LLM call
    needs bytes on disk, not just a truthy key.
    """
    out: list[str] = []
    for i, k in enumerate(keys or []):
        if not k or not await object_store.exists(k):
            continue
        p = await ws.fetch(k, name=f"{label}_{i}{Path(k).suffix or '.png'}")
        out.append(str(p))
    return out


async def _resolve_ff_context(shot: Shot) -> str | None:
    """first_frame/custom-first 的 context 帧：目标尾帧 → 实际尾帧 → 无。"""
    ctx = await resolve_tail_frame(shot.target_last_frame_path)
    if ctx:
        return ctx
    if shot.last_frame_path and await object_store.exists(shot.last_frame_path):
        return shot.last_frame_path
    return None


@observability.traced_job("worker-image-candidate-run", tags=["image-candidate"])
async def run_image_candidate(
    ctx: Dict[str, Any], project_id: str, shot_id: int, candidate_id: str, actor: str
) -> None:
    """统一图片候选生成：模式 = slot + 有无 custom_prompt；不触碰 project 状态机。

    app.services.image_generation 的生成函数只认本地文件路径（parts_from_paths
    对每个入参做 Path(p).exists()/read_bytes()，对 COS key 恒静默判"不存在"、
    悄悄从提示词里丢掉该参考图，不报任何错）。因此本函数必须先把全部参考图/
    上下文帧/pristine 帧 fetch 进一次性 workspace 再调用，产物 publish 回
    shot_candidates_prefix 下的新 key，绝不把裸 key 字符串递给 ig.*。
    """
    from app.services import image_generation as ig
    from app.services.storage import shot_candidates_prefix

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
        out_key: Optional[str] = None
        try:
            refs = json.loads(cand.ref_paths) if cand.ref_paths else {}
            if "character" in refs:
                char_refs = refs["character"]
            else:
                char_refs = await _get_character_ref_paths(project_id, session)
            obj_refs = refs.get("object") or None

            async with workspace() as ws:
                async def _fetch_all(keys: Optional[list[str]], label: str) -> list[str]:
                    if not keys:
                        return []
                    out_paths = []
                    for i, k in enumerate(keys):
                        p = await ws.fetch(k, name=f"{label}_{i}{Path(k).suffix or '.png'}")
                        out_paths.append(str(p))
                    return out_paths

                async def _fetch_one(key: Optional[str], name: str) -> Optional[str]:
                    if not key:
                        return None
                    p = await ws.fetch(key, name=f"{name}{Path(key).suffix or '.png'}")
                    return str(p)

                local_char_refs = await _fetch_all(char_refs, "char")
                local_obj_refs = await _fetch_all(obj_refs, "obj") if obj_refs else None
                local_out = str(ws.path("candidate_out.png"))

                if cand.slot == "cc":
                    # pristine_last_frame_key is the source of truth (never a
                    # directory scan); fall back to the current last_frame_path
                    # for shots that predate the column.
                    pristine_key = shot.pristine_last_frame_key or shot.last_frame_path
                    if pristine_key is None:
                        raise ValueError("Shot has no last frame to calibrate")
                    local_pristine = await _fetch_one(pristine_key, "pristine")
                    await ig.calibrate_face(local_char_refs, local_pristine, local_out)

                elif cand.custom_prompt:
                    if cand.slot == "tail_frame":
                        ff = await pick_first_frame(project_id, shot, session)
                        context_key = str(ff) if ff else None
                    else:
                        context_key = await _resolve_ff_context(shot)
                    local_context = await _fetch_one(context_key, "context")
                    await ig.generate_custom(
                        prompt=cand.custom_prompt,
                        output_path=local_out,
                        character_ref_paths=local_char_refs,
                        object_ref_paths=local_obj_refs,
                        context_frame_path=local_context,
                        aspect_ratio=project.aspect_ratio,
                    )

                elif cand.slot == "tail_frame":
                    first_frame = await pick_first_frame(project_id, shot, session)
                    local_first_frame = await _fetch_one(
                        str(first_frame) if first_frame else None, "first_frame"
                    )
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
                            # local_obj_refs (fetched into ws above) — obj_refs
                            # itself is a list of COS keys and would be silently
                            # dropped by director.run_director's Path.exists() (C2).
                            reference_image_paths=local_obj_refs,
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
                        character_ref_paths=local_char_refs,
                        first_frame_path=local_first_frame,
                        motion_prompt=motion_prompt,
                        output_path=local_out,
                        object_ref_paths=local_obj_refs,
                        aspect_ratio=project.aspect_ratio,
                        on_cot_complete=_on_cot,
                    )

                else:  # first_frame auto
                    context_key = await _resolve_ff_context(shot)
                    local_context = await _fetch_one(context_key, "context")
                    await ig.generate_first_frame(
                        character_ref_paths=local_char_refs,
                        context_frame_path=local_context,
                        visual_description=shot.visual_description,
                        shot_type=shot.shot_type,
                        output_path=local_out,
                        motion_prompt=shot.motion_prompt,
                        object_ref_paths=local_obj_refs,
                        aspect_ratio=project.aspect_ratio,
                    )

                out_key = f"{shot_candidates_prefix(project_id, shot_id)}{ts_uuid_name('.png')}"
                await ws.publish(Path(local_out), out_key)

            cand.file_path = out_key
            cand.status = "done"
            cand.error = None
            if cand.slot == "cc":
                # 若当前 last_frame 已是已采纳的校准帧（cc_*.png），保持 cc_status="done"
                # 以维持 已校准/还原 UI 与 character-calibrate-revert 可用；否则清空。
                shot.cc_status = (
                    "done"
                    if (shot.last_frame_path and Path(shot.last_frame_path).name.startswith("cc_"))
                    else None
                )
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
                        "file_path": to_media_url(out_key),
                    },
                })
            except Exception:
                logger.error(
                    "Failed to publish image_candidate_completed for %s", candidate_id,
                    exc_info=True,
                )


async def merge_project_shots(session_factory, project_id: str) -> str:
    """Bake every completed shot's effective clip (trim/VC/head-mute applied)
    into a workspace, concat them, and publish the result. Returns the final key.

    Merging pulls the whole project's videos to disk — a 20-shot project can
    reach several GB. Precheck with the REAL object sizes (object_store.size,
    not an estimate) before fetching: skip it and a shortfall shows up as an
    opaque ffmpeg failure deep inside the fetch/bake loop, very hard to
    diagnose after the fact.
    """
    from app.services.storage import final_video_key

    async with session_factory() as s:
        shots = (await s.execute(
            select(Shot)
            .where(Shot.project_id == project_id, Shot.status == ShotStatus.COMPLETED.value)
            .order_by(Shot.shot_id)
        )).scalars().all()

    shots = [sh for sh in shots if sh.video_path]
    if not shots:
        raise ValueError("No completed shots to merge")

    keys = [sh.video_path for sh in shots]
    keys += [sh.vc_audio_path for sh in shots if sh.vc_audio_path]
    total = sum([await object_store.size(k) for k in keys])
    await ensure_free_space(int(total * 2.2))  # inputs + baked output + margin

    async with workspace() as ws:
        specs: list[ClipSpec] = []
        for i, sh in enumerate(shots):
            # Fetching N shots into ONE workspace requires an explicit distinct
            # name per key — ws.fetch's same-local-name guard raises if two
            # different keys would default to the same last path segment
            # (every shot's video is "output_<ts>_<uuid>.mp4"-shaped).
            local_video = await ws.fetch(sh.video_path, name=f"part_{i:04d}.mp4")
            local_vc = None
            if sh.vc_audio_path:
                local_vc = str(await ws.fetch(sh.vc_audio_path, name=f"vc_{i:04d}.wav"))
            specs.append(ClipSpec(
                local_video_path=str(local_video),
                trim_frames=sh.trim_frames,
                local_vc_audio_path=local_vc,
                audio_head_mute_frames=sh.audio_head_mute_frames,
            ))

        clip_paths = effective_clip_paths(specs, str(ws.root))
        out = ws.path("merged.mp4")
        merge_shots(clip_paths, str(out))
        key = await ws.publish(out, final_video_key(project_id))

    async with session_factory() as s:
        proj = (await s.execute(
            select(Project).where(Project.id == project_id)
        )).scalar_one()
        proj.final_video_path = key
        await s.commit()
    return key


async def run_merger(
    ctx: Dict[str, Any],
    project_id: str,
    actor: str,
) -> None:
    """
    Merge all completed shots into final video.

    Args:
        ctx: arq context
        project_id: Project ID
        actor: Who triggered this
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

        try:
            await merge_project_shots(session_factory, project_id)
            # merge_project_shots committed project.final_video_path on a
            # separate session; refresh this one before the status transition.
            await session.refresh(project)

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


async def _do_voice_convert_one(
    session_factory,
    redis,
    project_id: str,
    shot_id: int,
    ref_audio_key: str,
) -> None:
    """Voice-convert a single shot using the given reference audio.

    Non-destructive model: shot.video_path is the single immutable source
    video (VC never depends on trim state — it always reads the full audio
    from the current video_path); the converted wav is published to a FIXED
    COS key (shot_audio_vc_key) that a later run simply overwrites, so there
    is no "drop the prior vc audio" bookkeeping to get wrong. video_path
    itself is never touched; only shot.vc_audio_path is updated
    (metadata-only).
    """
    from app.agents.audio_extractor import extract_audio_wav
    from app.services.cosyvoice_client import voice_convert
    from app.services.storage import shot_audio_vc_key

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
            # VC 是非破坏式的：video_path 指向不可变源，只另写 vc_audio_path，
            # voice-revert 清空该指针即可还原。因此不需要备份 VC 前的整片
            # ——旧的 ensure_pre_vc_backup 备份从来没有任何读取方。
            vc_key = shot_audio_vc_key(project_id, shot_id)
            async with workspace() as ws:
                local_video = await ws.fetch(shot.video_path, name="source.mp4")
                local_ref_audio = await ws.fetch(ref_audio_key, name="ref_prompt.wav")
                local_src_audio = ws.path("audio_in.wav")
                extract_audio_wav(str(local_video), str(local_src_audio))

                local_vc_audio = ws.path("audio_vc.wav")
                await voice_convert(str(local_src_audio), str(local_ref_audio), str(local_vc_audio))
                await ws.publish(local_vc_audio, vc_key)

            # Metadata only: video_path is NOT touched; it stays the source.
            shot.vc_audio_path = vc_key
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
                        "vc_audio_url": to_media_url(vc_key),
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
        ref_audio_key = await resolve_reference_prompt_wav(project_id, project, session)
        if ref_audio_key is None:
            logger.error("Project %s has no reference voice set", project_id)
            return

    await _do_voice_convert_one(session_factory, redis, project_id, shot_id, ref_audio_key)


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
        ref_audio_key = await resolve_reference_prompt_wav(project_id, project, session)
        if ref_audio_key is None:
            logger.error("Project %s has no reference voice set", project_id)
            return

    converted = 0
    failed = 0
    for sid in shot_ids:
        try:
            await _do_voice_convert_one(session_factory, redis, project_id, sid, ref_audio_key)
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
    """校准一个 shot 的 last frame → 产出 cc 候选（采纳后才替换，见 adopt 端点）。

    ref_image_paths 是角色参考图的 COS key 列表。
    """
    from app.services import image_generation as ig
    from app.services.storage import shot_candidates_prefix

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
            # 从未校准的 pristine 帧出发（绝不叠加已校准帧）——pristine_last_frame_key
            # 是唯一真相来源，不再目录扫描；缺失（旧数据）时退回当前 last_frame_path。
            pristine_key = shot.pristine_last_frame_key or shot.last_frame_path
            out_key = f"{shot_candidates_prefix(project_id, shot_id)}{ts_uuid_name('.png')}"

            async with workspace() as ws:
                local_refs = [
                    str(await ws.fetch(k, name=f"char_ref_{i}{Path(k).suffix or '.png'}"))
                    for i, k in enumerate(ref_image_paths)
                ]
                local_pristine = await ws.fetch(pristine_key, name="pristine_last_frame.png")
                local_out = ws.path("cc_candidate.png")
                await ig.calibrate_face(local_refs, str(local_pristine), str(local_out))
                await ws.publish(local_out, out_key)

            cand.file_path = out_key
            cand.status = "done"
            # 若当前 last_frame 已是已采纳的校准帧（cc_*.png），保持 cc_status="done"
            # 以维持 已校准/还原 UI 与 character-calibrate-revert 可用；否则清空。
            shot.cc_status = (
                "done"
                if (shot.last_frame_path and Path(shot.last_frame_path).name.startswith("cc_"))
                else None
            )
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
                        "file_path": to_media_url(out_key),
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
