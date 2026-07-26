"""图片候选端点 — 统一图片生成（生成→候选→采纳）。"""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.main import get_redis
from app.models.project import ImageCandidate, Project, ReferenceImage, Shot
from app.services.storage import (
    to_media_url, ts_uuid_name, shot_key,
    shot_candidates_prefix, shot_custom_frames_prefix,
)
from app.services import object_store
from app.services.workspace import workspace

logger = logging.getLogger(__name__)
router = APIRouter()

VALID_CREATE_SLOTS = {"first_frame", "tail_frame"}


def _require_user(x_user_name: Optional[str] = Header(default=None)) -> str:
    if not x_user_name:
        raise HTTPException(status_code=400, detail="X-User-Name header required")
    return x_user_name


async def _get_project_or_404(project_id: str, session: AsyncSession) -> Project:
    result = await session.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def _get_shot_or_404(project_id: str, shot_id: int, session: AsyncSession) -> Shot:
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")
    return shot


async def _get_candidate_or_404(
    project_id: str, shot_id: int, candidate_id: str, session: AsyncSession
) -> ImageCandidate:
    result = await session.execute(
        select(ImageCandidate).where(
            ImageCandidate.id == candidate_id,
            ImageCandidate.project_id == project_id,
            ImageCandidate.shot_id == shot_id,
        )
    )
    cand = result.scalar_one_or_none()
    if not cand:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return cand


async def _get_arq_redis(redis) -> ArqRedis:
    return ArqRedis(redis.connection_pool)


@router.post(
    "/projects/{project_id}/shots/{shot_id}/image-candidates", status_code=202
)
async def create_image_candidate(
    project_id: str,
    shot_id: int,
    slot: str = Form(...),
    custom_prompt: Optional[str] = Form(default=None),
    ref_image_ids: Optional[str] = Form(default=None),
    include_shot_refs: bool = Form(default=True),
    files: List[UploadFile] = File(default=[]),
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """创建一个图片候选并入队生成（每次 1 张；不触碰 project 状态机）。"""
    from app.api.projects import _candidate_to_dict

    if slot not in VALID_CREATE_SLOTS:
        raise HTTPException(
            status_code=400,
            detail=f"slot must be one of {sorted(VALID_CREATE_SLOTS)} (cc candidates are created by calibrate endpoints)",
        )
    await _get_project_or_404(project_id, session)
    shot = await _get_shot_or_404(project_id, shot_id, session)

    custom_prompt = (custom_prompt or "").strip() or None

    # ── 解析参考图选择 → ref_paths JSON ──
    # "character" 键只有在显式传了 ref_image_ids 时才写入（哪怕是空列表）；
    # 否则不写该键，worker 端按键缺失回退到默认角色参考图。
    selected_char: list[str] = []
    selected_obj: list[str] = []

    if ref_image_ids is not None:
        try:
            ids = json.loads(ref_image_ids)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="ref_image_ids must be a JSON array")
        if not isinstance(ids, list):
            raise HTTPException(status_code=400, detail="ref_image_ids must be a JSON array")
        if ids:
            rows = (await session.execute(
                select(ReferenceImage).where(
                    ReferenceImage.project_id == project_id,
                    ReferenceImage.id.in_(ids),
                )
            )).scalars().all()
            for r in rows:
                (selected_char if r.kind == "character" else selected_obj).append(r.storage_path)

    if include_shot_refs and shot.custom_reference_paths:
        selected_obj += json.loads(shot.custom_reference_paths)

    if files:
        prefix = shot_candidates_prefix(project_id, shot_id)
        async with workspace() as ws:
            for f in files:
                ext = Path(f.filename or "x.png").suffix or ".png"
                local = ws.path(f"ref_{ts_uuid_name(ext)}")
                local.write_bytes(await f.read())
                dest_key = f"{prefix}{local.name}"
                await ws.publish(local, dest_key)
                selected_obj.append(dest_key)

    refs: dict = {}
    if ref_image_ids is not None:
        refs["character"] = selected_char
    if ref_image_ids is not None or selected_obj:
        refs["object"] = selected_obj
    ref_paths = json.dumps(refs) if refs else None

    cand = ImageCandidate(
        project_id=project_id,
        shot_pk=shot.id,
        shot_id=shot_id,
        slot=slot,
        status="generating",
        prompt_source="custom" if custom_prompt else "auto",
        custom_prompt=custom_prompt,
        ref_paths=ref_paths,
    )
    session.add(cand)
    await session.commit()
    await session.refresh(cand)

    arq = await _get_arq_redis(redis)
    await arq.enqueue_job("run_image_candidate", project_id, shot_id, cand.id, f"user:{user}")

    return {"status": "queued", "candidate": _candidate_to_dict(cand)}


@router.delete("/projects/{project_id}/shots/{shot_id}/image-candidates/{candidate_id}")
async def delete_image_candidate(
    project_id: str,
    shot_id: int,
    candidate_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """删除候选：删行 + unlink 文件。生成中（未超时）禁删；卡死（>=30min）可删。已采纳可删（槽位持副本）。"""
    await _get_project_or_404(project_id, session)
    cand = await _get_candidate_or_404(project_id, shot_id, candidate_id, session)
    if cand.status == "generating" and (datetime.utcnow() - cand.created_at) < timedelta(minutes=30):
        raise HTTPException(status_code=409, detail="Candidate is still generating")
    # 素材审计（CLAUDE.md）：先删行（解除引用）再删 COS 对象。
    file_key = cand.file_path
    await session.delete(cand)
    await session.commit()
    if file_key:
        await object_store.delete(file_key)
    return {"deleted": candidate_id}


async def adopt_candidate_to_last_frame(
    session_factory, project_id: str, shot_id: int, candidate_key: str
) -> str:
    """采纳候选图为分镜尾帧（角色校准）。返回新的 last_frame key。

    注意：本函数覆盖 last_frame_path，但**绝不**触碰 pristine_last_frame_key
    ——后者是 CC 还原的唯一目标（worker/tasks.py、app/api/pipeline.py 的
    character-calibrate-revert 端点都以它为准）。

    备份/入槽全程用 COS 服务端 copy（零流量）。旧的已采纳校准帧（若存在）
    在 DB 提交之后才删除——先解除引用再删对象，符合素材审计的一致性约定。
    """
    from app.services.first_frame import propagate_first_frame_to_next

    dest_key = shot_key(project_id, shot_id, f"cc_{ts_uuid_name('.png')}")
    await object_store.copy(candidate_key, dest_key)

    async with session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
        )).scalar_one()
        stale_cc_key = shot.last_frame_path
        if not (stale_cc_key and Path(stale_cc_key).name.startswith("cc_")):
            stale_cc_key = None

        shot.last_frame_path = dest_key
        shot.cc_status = "done"
        shot.cc_error_message = None
        await propagate_first_frame_to_next(project_id, shot, dest_key, s)
        await s.commit()

    if stale_cc_key:
        await object_store.delete(stale_cc_key)

    return dest_key


@router.post("/projects/{project_id}/shots/{shot_id}/image-candidates/{candidate_id}/adopt")
async def adopt_image_candidate(
    project_id: str,
    shot_id: int,
    candidate_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """采纳候选：复制入槽（路径即真相），同槽位其他候选取消采纳标记。"""
    from app.api.projects import _candidate_to_dict

    await _get_project_or_404(project_id, session)
    shot = await _get_shot_or_404(project_id, shot_id, session)
    cand = await _get_candidate_or_404(project_id, shot_id, candidate_id, session)

    if cand.status != "done" or not cand.file_path or not await object_store.exists(cand.file_path):
        raise HTTPException(status_code=400, detail="Candidate is not ready to adopt")

    src_key = cand.file_path
    extra: dict = {}

    if cand.slot == "first_frame":
        dest_key = f"{shot_custom_frames_prefix(project_id, shot_id)}{ts_uuid_name(Path(src_key).suffix or '.png')}"
        await object_store.copy(src_key, dest_key)
        shot.custom_first_frame_path = dest_key
        extra["custom_first_frame_path"] = to_media_url(dest_key)
    elif cand.slot == "tail_frame":
        dest_key = shot_key(project_id, shot_id, ts_uuid_name(Path(src_key).suffix or ".png"))
        await object_store.copy(src_key, dest_key)
        shot.target_last_frame_path = dest_key
        shot.tf_status = "done"
        extra["target_last_frame_path"] = to_media_url(dest_key)
    elif cand.slot == "cc":
        from app.db import AsyncSession as session_factory

        dest_key = await adopt_candidate_to_last_frame(
            session_factory, project_id, shot_id, src_key
        )
        # adopt_candidate_to_last_frame committed on ITS OWN session — refresh
        # the outer `shot` object so the add_all([cand, shot]) commit below
        # doesn't flush stale in-memory values back over the just-committed row.
        await session.refresh(shot)
        extra["last_frame_path"] = to_media_url(dest_key)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown slot: {cand.slot}")

    # 同槽位互斥采纳标记
    siblings = (await session.execute(
        select(ImageCandidate).where(
            ImageCandidate.project_id == project_id,
            ImageCandidate.shot_id == shot_id,
            ImageCandidate.slot == cand.slot,
        )
    )).scalars().all()
    for c in siblings:
        c.adopted_at = None
        session.add(c)
    cand.adopted_at = datetime.utcnow()
    session.add_all([cand, shot])
    await session.commit()
    await session.refresh(cand)

    return {"shot_id": shot_id, "slot": cand.slot, "candidate": _candidate_to_dict(cand), **extra}
