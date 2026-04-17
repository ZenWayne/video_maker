"""Projects API routes."""

import json
import shutil
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.models.project import Project, Shot, ReferenceImage
from app.models.schemas import (
    ProjectCreate, ProjectResponse, ProjectList, ProjectListResponse,
    Storyboard, ErrorResponse
)
from app.services.storage import project_dir, delete_project_storage, to_media_url
from app.services.state_machine import ProjectStatus, InvalidTransitionError
from app.main import get_redis

router = APIRouter()


def _require_user(x_user_name: Optional[str] = Header(default=None)) -> str:
    """Require X-User-Name header."""
    if not x_user_name:
        raise HTTPException(status_code=400, detail="X-User-Name header required")
    return x_user_name


@router.get("/projects", response_model=ProjectList)
async def list_projects(
    status: Optional[str] = Query(default=None),
    creator: Optional[str] = Query(default=None),
    sort: str = Query(default="created_at:desc"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
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
    if creator:
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
    session: AsyncSession = Depends(get_session),
):
    """Create a new project."""
    project = Project(
        title=body.title,
        theme_text=body.theme_text,
        creator_name=user,
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
    storyboard = None
    if project.storyboard_path:
        import json
        from pathlib import Path
        try:
            sb_data = json.loads(Path(project.storyboard_path).read_text())
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
        shots=[
            {
                "id": s.id,
                "shot_id": s.shot_id,
                "text": s.text,
                "shot_type": s.shot_type,
                "visual_description": s.visual_description,
                "shot_duration": s.shot_duration,
                "status": s.status,
                "align_with_previous": s.align_with_previous,
                "motion_prompt": s.motion_prompt,
                "first_frame_path": to_media_url(s.first_frame_path),
                "video_path": to_media_url(s.video_path),
                "last_frame_path": to_media_url(s.last_frame_path),
                "word_count_warning": s.word_count_warning,
                "error_message": s.error_message,
                "custom_first_frame_path": to_media_url(s.custom_first_frame_path),
                "custom_reference_paths": (
                    [to_media_url(p) for p in json.loads(s.custom_reference_paths)]
                    if s.custom_reference_paths else None
                ),
                "reference_image_hint": s.reference_image_hint,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
            }
            for s in project.shots
        ],
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
    storyboard = None
    if project.storyboard_path:
        import json
        from pathlib import Path
        try:
            sb_data = json.loads(Path(project.storyboard_path).read_text())
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
        shots=[
            {
                "id": s.id,
                "shot_id": s.shot_id,
                "text": s.text,
                "shot_type": s.shot_type,
                "visual_description": s.visual_description,
                "shot_duration": s.shot_duration,
                "status": s.status,
                "align_with_previous": s.align_with_previous,
                "motion_prompt": s.motion_prompt,
                "first_frame_path": to_media_url(s.first_frame_path),
                "video_path": to_media_url(s.video_path),
                "last_frame_path": to_media_url(s.last_frame_path),
                "word_count_warning": s.word_count_warning,
                "error_message": s.error_message,
                "custom_first_frame_path": to_media_url(s.custom_first_frame_path),
                "custom_reference_paths": (
                    [to_media_url(p) for p in json.loads(s.custom_reference_paths)]
                    if s.custom_reference_paths else None
                ),
                "reference_image_hint": s.reference_image_hint,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
            }
            for s in project.shots
        ],
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

    # Delete from database (cascade will handle related records)
    await session.delete(project)
    await session.commit()

    # Delete storage
    delete_project_storage(project_id)

    return None
