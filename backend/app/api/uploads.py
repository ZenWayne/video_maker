"""Uploads API routes for reference images."""

import uuid
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.project import Project, ReferenceImage
from app.models.schemas import ReferenceImageResponse
from app.services import object_store
from app.services.storage import reference_image_key
from app.services.workspace import workspace

router = APIRouter()


@router.post(
    "/projects/{project_id}/reference-images",
    response_model=List[ReferenceImageResponse],
    status_code=201,
)
async def upload_reference_images(
    project_id: str,
    kind: str = Form(...),
    files: List[UploadFile] = File(...),
    session: AsyncSession = Depends(get_session),
):
    """Upload reference images for a project."""
    if kind not in ("character", "scene"):
        raise HTTPException(
            status_code=400, detail="kind must be 'character' or 'scene'"
        )

    # Check project exists
    result = await session.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # Get current count for this kind
    existing = await session.execute(
        select(ReferenceImage).where(
            ReferenceImage.project_id == project_id,
            ReferenceImage.kind == kind,
        )
    )
    current_count = len(existing.scalars().all())

    created = []
    async with workspace() as ws:
        for idx, upload in enumerate(files):
            # Read file content
            content = await upload.read()

            # Generate safe filename
            safe_name = Path(upload.filename).name if upload.filename else f"image_{idx}.bin"
            image_id = str(uuid.uuid4())[:8]

            # Stage locally then publish to COS (put succeeds before the DB
            # row is created — consistency invariant: new files put first).
            local = ws.path(f"ref_{idx}_{safe_name}")
            local.write_bytes(content)
            key = await ws.publish(local, reference_image_key(project_id, image_id, safe_name))

            # Create database record
            img = ReferenceImage(
                project_id=project_id,
                kind=kind,
                filename=safe_name,
                storage_path=key,
                order_index=current_count + idx,
            )
            session.add(img)
            created.append(img)

    await session.commit()
    for img in created:
        await session.refresh(img)

    return [
        ReferenceImageResponse(
            id=img.id,
            kind=img.kind,
            filename=img.filename,
            storage_path=img.storage_path,
            order_index=img.order_index,
            created_at=img.created_at,
        )
        for img in created
    ]


@router.delete(
    "/projects/{project_id}/reference-images/{image_id}", status_code=204
)
async def delete_reference_image(
    project_id: str,
    image_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Delete a reference image."""
    result = await session.execute(
        select(ReferenceImage).where(
            ReferenceImage.id == image_id,
            ReferenceImage.project_id == project_id,
        )
    )
    img = result.scalar_one_or_none()
    if img is None:
        raise HTTPException(status_code=404, detail="Image not found")

    # 素材审计（CLAUDE.md）：先删行（解除引用）再删 COS 对象。
    key = img.storage_path
    await session.delete(img)
    await session.commit()

    if key:
        await object_store.delete(key)

    return None
