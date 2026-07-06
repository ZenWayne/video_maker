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
from app.services.storage import shot_candidates_dir, to_media_url, ts_uuid_name

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
        cand_dir = shot_candidates_dir(project_id, shot_id)
        cand_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            ext = Path(f.filename or "x.png").suffix or ".png"
            dest = cand_dir / f"ref_{ts_uuid_name(ext)}"
            dest.write_bytes(await f.read())
            selected_obj.append(str(dest))

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
    if cand.file_path:
        Path(cand.file_path).unlink(missing_ok=True)
    await session.delete(cand)
    await session.commit()
    return {"deleted": candidate_id}


@router.post("/projects/{project_id}/shots/{shot_id}/image-candidates/{candidate_id}/adopt")
async def adopt_image_candidate(
    project_id: str,
    shot_id: int,
    candidate_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """采纳候选：复制入槽（路径即真相），同槽位其他候选取消采纳标记。"""
    import shutil
    from app.api.projects import _candidate_to_dict
    from app.services.first_frame import propagate_first_frame_to_next
    from app.services.storage import shot_custom_frames_dir, shot_dir

    await _get_project_or_404(project_id, session)
    shot = await _get_shot_or_404(project_id, shot_id, session)
    cand = await _get_candidate_or_404(project_id, shot_id, candidate_id, session)

    if cand.status != "done" or not cand.file_path or not Path(cand.file_path).exists():
        raise HTTPException(status_code=400, detail="Candidate is not ready to adopt")

    src = Path(cand.file_path)
    extra: dict = {}

    if cand.slot == "first_frame":
        dest_dir = shot_custom_frames_dir(project_id, shot_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / ts_uuid_name(src.suffix or ".png")
        shutil.copy2(src, dest)
        shot.custom_first_frame_path = str(dest)
        extra["custom_first_frame_path"] = to_media_url(str(dest))
    elif cand.slot == "tail_frame":
        dest_dir = shot_dir(project_id, shot_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / ts_uuid_name(src.suffix or ".png")
        shutil.copy2(src, dest)
        shot.target_last_frame_path = str(dest)
        shot.tf_status = "done"
        extra["target_last_frame_path"] = to_media_url(str(dest))
    elif cand.slot == "cc":
        s_dir = shot_dir(project_id, shot_id)
        s_dir.mkdir(parents=True, exist_ok=True)
        dest = s_dir / f"cc_{ts_uuid_name('.png')}"
        shutil.copy2(src, dest)
        # 旧校准帧只留最新（沿用原 CC worker 行为）；pristine last_frame_* 不动
        for _old in s_dir.glob("cc_*.png"):
            if _old != dest:
                _old.unlink(missing_ok=True)
        shot.last_frame_path = str(dest)
        shot.cc_status = "done"
        shot.cc_error_message = None
        await propagate_first_frame_to_next(project_id, shot, str(dest), session)
        extra["last_frame_path"] = to_media_url(str(dest))
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
