"""Projects API routes."""

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.identity import current_principal, require_user
from app.config import settings
from app.db import get_session
from app.models.project import Project, Shot, ReferenceImage
from app.models.schemas import (
    ProjectCreate, ProjectResponse, ProjectList, ProjectListResponse,
    Storyboard, ErrorResponse
)
# 注：project_dir 是死 import（正文从未使用）。delete_project_storage（本地
# 目录级删除，COS 下不成立）曾是本文件唯一的裸名 NameError 陷阱——Task 11
# （导出/删除/storyboard）已修复：delete_project 改为「先删 DB 行再
# object_store.delete_prefix(project_prefix(...))」；create_project/
# get_project 的 storyboard 读取改用 app.services.storyboard.read_storyboard
# （project.storyboard_path 现在存的是 COS key，不再是本地路径，
# Path(...).read_text() 对 key 恒抛 FileNotFoundError）。
from app.services import object_store
from app.services.storage import to_media_url, project_prefix
from app.services.storyboard import read_storyboard
from app.services.state_machine import ProjectStatus, InvalidTransitionError

router = APIRouter()


def _candidate_to_dict(c) -> dict:
    """Serialize an ImageCandidate for the API."""
    return {
        "id": c.id,
        "shot_id": c.shot_id,
        "slot": c.slot,
        "status": c.status,
        "file_path": to_media_url(c.file_path),
        "prompt_source": c.prompt_source,
        "custom_prompt": c.custom_prompt,
        "error": c.error,
        "created_at": c.created_at,
        "adopted_at": c.adopted_at,
    }


def _shot_to_dict(s) -> dict:
    """Serialize a Shot for the API, including the non-destructive playback descriptor."""
    trim_end_sec = None
    if s.trim_frames and s.source_fps:
        trim_end_sec = s.trim_frames / s.source_fps
    audio_head_mute_sec = None
    if s.audio_head_mute_frames and s.source_fps:
        audio_head_mute_sec = s.audio_head_mute_frames / s.source_fps
    return {
        "id": s.id,
        "shot_id": s.shot_id,
        "text": s.text,
        "shot_type": s.shot_type,
        "visual_description": s.visual_description,
        "shot_duration": s.shot_duration,
        "status": s.status,
        "align_with_previous": s.align_with_previous,
        "motion_prompt": s.motion_prompt,
        "video_path": to_media_url(s.video_path),
        "last_frame_path": to_media_url(s.last_frame_path),
        "word_count_warning": s.word_count_warning,
        "error_message": s.error_message,
        "custom_first_frame_path": to_media_url(s.custom_first_frame_path),
        "ff_status": s.ff_status,
        "ff_error_message": s.ff_error_message,
        "custom_reference_paths": (
            [to_media_url(p) for p in json.loads(s.custom_reference_paths)]
            if s.custom_reference_paths else None
        ),
        "reference_image_hint": s.reference_image_hint,
        "use_prev_last_frame": s.use_prev_last_frame,
        "vc_status": s.vc_status,
        "vc_error_message": s.vc_error_message,
        "cc_status": s.cc_status,
        "cc_error_message": s.cc_error_message,
        "target_last_frame_path": to_media_url(s.target_last_frame_path),
        "tf_status": s.tf_status,
        "tf_error_message": s.tf_error_message,
        "tf_confirmed": bool(s.tf_confirmed),
        "image_candidates": [_candidate_to_dict(c) for c in s.image_candidates],
        # --- 非破坏式播放描述 ---
        "trim_frames": s.trim_frames,
        "source_fps": s.source_fps,
        "source_frames": s.source_frames,
        "trim_end_sec": trim_end_sec,
        "audio_head_mute_frames": s.audio_head_mute_frames,
        "audio_head_mute_sec": audio_head_mute_sec,
        "vc_audio_url": to_media_url(s.vc_audio_path),
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }


# 兼容旧调用点的别名；实现见 app.api.identity（会话优先，X-User-Name 回落）。
_require_user = require_user


@router.get("/projects", response_model=ProjectList)
async def list_projects(
    status: Optional[str] = Query(default=None),
    creator: Optional[str] = Query(default=None),
    sort: str = Query(default="created_at:desc"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal=Depends(current_principal),
    session: AsyncSession = Depends(get_session),
):
    """List projects with filtering and pagination."""
    # Build query with eager loading
    query = select(Project).options(
        selectinload(Project.shots),
        selectinload(Project.reference_images)
    )

    if status:
        query = query.where(Project.status == status)

    if principal is not None and principal.is_billable:
        # 已登录：按会话身份过滤，**忽略 creator 查询参数** —— 把过滤条件交给
        # 调用方等于没有过滤。注意仅过滤列表是不够的，逐对象归属校验在
        # app.auth_middleware 里（知道 id 也拿不到别人的项目）。
        conditions = [Project.owner_id == principal.user_id]
        if not settings.auth_enforced:
            # 强制校验打开之前，存量未迁移数据（owner_id IS NULL）仍然可见，
            # 否则 P2 阶段一登录就是空列表。P3 迁移后不再有 NULL 行。
            conditions.append(Project.owner_id.is_(None))
        query = query.where(or_(*conditions))
    elif creator:
        # 匿名调用（仅 AUTH_ENFORCED=false 时可能）保持鉴权上线前的行为。
        query = query.where(Project.creator_name == creator)

    # Sorting
    if sort == "created_at:desc":
        query = query.order_by(Project.created_at.desc())
    elif sort == "created_at:asc":
        query = query.order_by(Project.created_at.asc())
    elif sort == "updated_at:desc":
        query = query.order_by(Project.updated_at.desc())
    elif sort == "updated_at:asc":
        query = query.order_by(Project.updated_at.asc())

    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await session.execute(count_query)
    total = total_result.scalar()

    # Apply pagination
    query = query.limit(limit).offset(offset)

    result = await session.execute(query)
    projects = result.scalars().all()

    # Build response items
    items = []
    for p in projects:
        # Count shots
        shot_count = len(p.shots)
        completed_shot_count = sum(1 for s in p.shots if s.status == "completed")

        items.append(ProjectListResponse(
            id=p.id,
            title=p.title,
            theme_text=p.theme_text,
            aspect_ratio=p.aspect_ratio,
            creator_name=p.creator_name,
            status=p.status,
            scene_overview=p.scene_overview,
            final_video_path=p.final_video_path,
            error_message=p.error_message,
            created_at=p.created_at,
            updated_at=p.updated_at,
            shot_count=shot_count,
            completed_shot_count=completed_shot_count,
        ))

    return ProjectList(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/projects", response_model=ProjectResponse, status_code=201)
async def create_project(
    body: ProjectCreate,
    user: str = Depends(_require_user),
    principal=Depends(current_principal),
    session: AsyncSession = Depends(get_session),
):
    """Create a new project."""
    project = Project(
        title=body.title,
        theme_text=body.theme_text,
        creator_name=user,
        # 归属以 owner_id 为准；匿名创建（AUTH_ENFORCED=false）留 NULL，与存量
        # 数据同处境，由 P3 的迁移脚本一并归到 stella 名下。
        owner_id=principal.user_id if (principal and principal.is_billable) else None,
        status=ProjectStatus.DRAFT.value,
        aspect_ratio=body.aspect_ratio,
    )
    session.add(project)
    await session.commit()

    # Reload project with relationships
    result = await session.execute(
        select(Project)
        .where(Project.id == project.id)
        .options(selectinload(Project.shots), selectinload(Project.reference_images))
    )
    project = result.scalar_one()

    # Load storyboard if exists
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
        aspect_ratio=project.aspect_ratio,
        creator_name=project.creator_name,
        status=project.status,
        scene_overview=project.scene_overview,
        storyboard_path=project.storyboard_path,
        final_video_path=project.final_video_path,
        error_message=project.error_message,
        created_at=project.created_at,
        updated_at=project.updated_at,
        reference_images=[
            {
                "id": r.id,
                "kind": r.kind,
                "filename": r.filename,
                "storage_path": r.storage_path,
                "order_index": r.order_index,
                "created_at": r.created_at,
            }
            for r in project.reference_images
        ],
        shots=[_shot_to_dict(s) for s in project.shots],
        reference_voice_shot_id=project.reference_voice_shot_id,
        reference_voice_path=to_media_url(project.reference_voice_path),
        auto_voice_calibrate=project.auto_voice_calibrate,
        content_analysis_id=project.content_analysis_id,
        attached_brief_json=project.attached_brief_json,
        storyboard=storyboard,
    )


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Get a project by ID."""
    result = await session.execute(
        select(Project)
        .where(Project.id == project_id)
        .options(selectinload(Project.shots), selectinload(Project.reference_images))
    )
    project = result.scalar_one_or_none()

    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # Load storyboard if exists
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
        aspect_ratio=project.aspect_ratio,
        creator_name=project.creator_name,
        status=project.status,
        scene_overview=project.scene_overview,
        storyboard_path=project.storyboard_path,
        final_video_path=project.final_video_path,
        error_message=project.error_message,
        created_at=project.created_at,
        updated_at=project.updated_at,
        reference_images=[
            {
                "id": r.id,
                "kind": r.kind,
                "filename": r.filename,
                "storage_path": r.storage_path,
                "order_index": r.order_index,
                "created_at": r.created_at,
            }
            for r in project.reference_images
        ],
        shots=[_shot_to_dict(s) for s in project.shots],
        reference_voice_shot_id=project.reference_voice_shot_id,
        reference_voice_path=to_media_url(project.reference_voice_path),
        auto_voice_calibrate=project.auto_voice_calibrate,
        content_analysis_id=project.content_analysis_id,
        attached_brief_json=project.attached_brief_json,
        storyboard=storyboard,
    )


@router.get("/projects/{project_id}/script")
async def get_script(
    project_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Get just the script (scene_overview + shots text) for a project."""
    result = await session.execute(
        select(Project)
        .where(Project.id == project_id)
        .options(selectinload(Project.shots))
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    shots_result = await session.execute(
        select(Shot).where(Shot.project_id == project_id).order_by(Shot.shot_id)
    )
    shots = shots_result.scalars().all()

    return {
        "project_id": project.id,
        "title": project.title,
        "status": project.status,
        "theme_text": project.theme_text,
        "scene_overview": project.scene_overview,
        "shots": [
            {
                "shot_id": s.shot_id,
                "text": s.text,
                "shot_type": s.shot_type,
                "visual_description": s.visual_description,
                "shot_duration": s.shot_duration,
                "align_with_previous": s.align_with_previous,
                "word_count_warning": s.word_count_warning,
            }
            for s in shots
        ],
    }


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(
    project_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Delete a project and all associated data."""
    result = await session.execute(
        select(Project)
        .where(Project.id == project_id)
        .options(selectinload(Project.shots), selectinload(Project.reference_images))
    )
    project = result.scalar_one_or_none()

    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # Delete from database first (cascade handles shots/reference_images/events)
    # so nothing can reference the COS objects before we clear them.
    await session.delete(project)
    await session.commit()

    # Clear the whole project prefix in COS (shots, storyboard(s), previews,
    # final video, reference images/voice). delete_prefix returns the count it
    # actually removed — a partial failure logs an error but doesn't raise
    # (consistency rule: an orphaned object is acceptable, a DB row pointing at
    # a deleted object is not — and the DB row is already gone by this point).
    await object_store.delete_prefix(project_prefix(project_id))

    return None
