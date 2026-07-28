"""内容分析 API：建分析+上传参考视频、列表、详情、SSE、挂载 brief 到 project。"""

import json
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from arq.connections import ArqRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.db import AsyncSession as session_factory
from app.main import get_redis
from app.api.projects import _require_user
from app.agents.asr import get_supported_language_codes
from app.models.project import ContentAnalysis, ReferenceSample, Project
from app.models.schemas import (
    ContentAnalysisResponse, ContentAnalysisList, AttachBriefRequest,
)
from app.models.schemas import ProjectResponse
from app.services.storage import sample_video_key
from app.services.workspace import workspace
from app.services.events import subscribe_to_events

router = APIRouter()


def _validate_region_hint(region_hint: Optional[str]) -> Optional[str]:
    """Empty/absent → None. Otherwise must be a language code the local ASR
    model (faster-whisper) accepts via its ``language=`` kwarg — NOT a
    free-text region. ``region_hint`` is passed straight through to
    faster-whisper, which raises ValueError on anything it doesn't recognize
    (e.g. "en-US", "美国") — that crash currently surfaces to the user as a
    misleading "无可用样本" analysis failure for every sample, with no retry
    endpoint. Validate at the API boundary instead, where a clear 400 can
    name the offending value."""
    value = (region_hint or "").strip()
    if not value:
        return None
    if value not in get_supported_language_codes():
        raise HTTPException(
            status_code=400,
            detail=(
                f"region_hint 不是 ASR 模型支持的语言代码：{value!r}。"
                "期望格式如 en / zh / ja（留空表示自动检测语言）。"
            ),
        )
    return value


async def _get_arq_redis(redis) -> ArqRedis:
    return ArqRedis(redis.connection_pool)


@router.post("/analyses", response_model=ContentAnalysisResponse, status_code=201)
async def create_analysis(
    title: str = Form(...),
    region_hint: Optional[str] = Form(default=None),
    files: List[UploadFile] = File(...),
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    if not files:
        raise HTTPException(status_code=400, detail="至少上传一个参考视频")
    region_hint = _validate_region_hint(region_hint)
    analysis = ContentAnalysis(title=title, region_hint=region_hint)
    session.add(analysis)
    await session.flush()  # 拿 analysis.id

    async with workspace() as ws:
        for idx, upload in enumerate(files):
            smp = ReferenceSample(analysis_id=analysis.id, order_index=idx, video_path="")
            session.add(smp)
            await session.flush()  # 拿 smp.id
            safe_name = (upload.filename or f"sample_{idx}.mp4").replace("/", "_")
            local = ws.path(f"sample_{smp.id}_{safe_name}")
            local.write_bytes(await upload.read())
            # Stage locally then publish to COS (put succeeds before the DB
            # row commits — consistency invariant: new files put first).
            key = await ws.publish(local, sample_video_key(analysis.id, smp.id, f"source_{safe_name}"))
            smp.video_path = key

    await session.commit()
    await session.refresh(analysis)

    arq = await _get_arq_redis(redis)
    await arq.enqueue_job("run_content_analysis", analysis.id, f"user:{user}")
    return analysis


@router.get("/analyses", response_model=ContentAnalysisList)
async def list_analyses(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(ContentAnalysis).order_by(ContentAnalysis.created_at.desc())
    )).scalars().all()
    return ContentAnalysisList(analyses=rows, total=len(rows))


@router.get("/analyses/{analysis_id}", response_model=ContentAnalysisResponse)
async def get_analysis(analysis_id: str, session: AsyncSession = Depends(get_session)):
    row = (await session.execute(
        select(ContentAnalysis).where(ContentAnalysis.id == analysis_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="分析不存在")
    return row


async def _content_analysis_stream_generator(redis, analysis_id: str):
    """Mirrors app/api/stream.py's event_generator: snapshot first (via its
    own short-lived session, released before the long-lived redis subscribe),
    then live events. A client connecting after the worker already finished
    still learns the terminal state instead of getting a silent open stream.
    """
    async with session_factory() as session:
        row = (await session.execute(
            select(ContentAnalysis).where(ContentAnalysis.id == analysis_id)
        )).scalar_one_or_none()
        if row is None:
            yield json.dumps({"type": "error", "message": "分析不存在"})
            return
        snapshot = {
            "type": "state_snapshot",
            "data": ContentAnalysisResponse.model_validate(row).model_dump(mode="json"),
        }

    yield json.dumps(snapshot)

    async for event in subscribe_to_events(redis, analysis_id):
        yield json.dumps(event)


@router.get("/analyses/{analysis_id}/stream")
async def stream_analysis(analysis_id: str, redis=Depends(get_redis)):
    from sse_starlette.sse import EventSourceResponse

    async with session_factory() as session:
        row = (await session.execute(
            select(ContentAnalysis).where(ContentAnalysis.id == analysis_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="分析不存在")

    return EventSourceResponse(
        _content_analysis_stream_generator(redis, analysis_id),
        media_type="text/event-stream",
    )


@router.post("/projects/{project_id}/attach-brief", response_model=ProjectResponse)
async def attach_brief(
    project_id: str,
    body: AttachBriefRequest,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    project = (await session.execute(
        select(Project).where(Project.id == project_id)
    )).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    analysis = (await session.execute(
        select(ContentAnalysis).where(ContentAnalysis.id == body.analysis_id)
    )).scalar_one_or_none()
    if analysis is None:
        raise HTTPException(status_code=404, detail="分析不存在")
    if not analysis.brief_json:
        raise HTTPException(status_code=409, detail="该分析尚未产出 brief")
    project.content_analysis_id = analysis.id
    project.attached_brief_json = analysis.brief_json  # 快照
    session.add(project)
    await session.commit()
    await session.refresh(project)

    # 与 pipeline.py 的 patch/put storyboard 端点一致：这里不需要向前端回传完整的
    # shots/reference_images 关系集合（未 selectin 预加载），手工构造响应，避免在
    # AsyncSession 上下文外触发同步懒加载（MissingGreenlet）。
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
        content_analysis_id=project.content_analysis_id,
        attached_brief_json=project.attached_brief_json,
        reference_images=[],
        shots=[],
        storyboard=None,
    )
