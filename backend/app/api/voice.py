"""Voice cloning / 音色校准 API routes.

Extracted from pipeline.py to keep that module focused. Covers the project
base-voice (uploaded file or marked shot), the auto-calibration switch, and
per-shot / batch voice conversion + revert. The actual conversion runs in the
``arq:vc`` worker queue; see ``app.services.reference_voice`` for the resolver
that decides which prompt wav a conversion uses.
"""

import shutil
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.main import get_redis
from app.models.project import Shot
from app.models.schemas import ReferenceVoiceRequest, AutoVoiceCalibrateRequest
from app.services.state_machine import ShotStatus
from app.services.storage import to_media_url, shot_pre_vc_video_path
from app.api.pipeline import _require_user, _get_project_or_404, _get_arq_redis

router = APIRouter()


@router.post("/projects/{project_id}/reference-voice")
async def set_reference_voice(
    project_id: str,
    body: ReferenceVoiceRequest,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Set the reference voice shot for voice cloning."""
    project = await _get_project_or_404(project_id, session)

    # Validate shot exists and is completed
    result = await session.execute(
        select(Shot).where(
            Shot.project_id == project_id, Shot.shot_id == body.shot_id
        )
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")
    if shot.status != ShotStatus.COMPLETED.value:
        raise HTTPException(status_code=400, detail="Shot must be completed")
    if not shot.video_path:
        raise HTTPException(status_code=400, detail="Shot has no video")

    project.reference_voice_shot_id = body.shot_id
    project.reference_voice_path = None  # mutual exclusivity
    project.updated_at = datetime.utcnow()
    session.add(project)
    await session.commit()

    return {"reference_voice_shot_id": body.shot_id, "reference_voice_path": None}


@router.delete("/projects/{project_id}/reference-voice")
async def clear_reference_voice(
    project_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Clear the reference voice setting."""
    project = await _get_project_or_404(project_id, session)

    project.reference_voice_shot_id = None
    project.reference_voice_path = None
    project.auto_voice_calibrate = False  # no base voice ⇒ auto cannot run
    project.updated_at = datetime.utcnow()
    session.add(project)
    await session.commit()

    return {"reference_voice_shot_id": None, "reference_voice_path": None,
            "auto_voice_calibrate": False}


@router.post("/projects/{project_id}/reference-voice/upload")
async def upload_reference_voice(
    project_id: str,
    file: UploadFile = File(...),
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Upload mp4/m4a/wav as the project base voice; normalize to prompt.wav."""
    import subprocess
    from app.services.reference_voice import (
        reference_voice_dir, reference_voice_prompt_path,
        has_audio_stream, normalize_reference_voice,
    )

    project = await _get_project_or_404(project_id, session)

    ext = Path(file.filename or "").suffix.lower()
    if ext not in {".mp4", ".m4a", ".wav"}:
        raise HTTPException(status_code=400, detail="Unsupported file type (use mp4/m4a/wav)")

    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (max 50MB)")

    reference_voice_dir(project_id).mkdir(parents=True, exist_ok=True)
    tmp_in = reference_voice_dir(project_id) / f"upload{ext}"
    tmp_in.write_bytes(data)
    out = reference_voice_prompt_path(project_id)
    try:
        if not has_audio_stream(str(tmp_in)):
            raise HTTPException(status_code=400, detail="File has no audio stream")
        normalize_reference_voice(str(tmp_in), str(out))
    except subprocess.CalledProcessError:
        raise HTTPException(status_code=400, detail="Failed to decode audio from file")
    finally:
        if tmp_in.exists():
            tmp_in.unlink()

    project.reference_voice_path = str(out)
    project.reference_voice_shot_id = None  # mutual exclusivity
    project.updated_at = datetime.utcnow()
    session.add(project)
    await session.commit()

    return {"reference_voice_path": to_media_url(str(out)), "reference_voice_shot_id": None}


@router.post("/projects/{project_id}/auto-voice-calibrate")
async def set_auto_voice_calibrate(
    project_id: str,
    body: AutoVoiceCalibrateRequest,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Toggle the project-level auto voice-calibration switch."""
    from app.services.reference_voice import resolve_reference_prompt_wav

    project = await _get_project_or_404(project_id, session)
    if body.enabled and resolve_reference_prompt_wav(project_id, project) is None:
        raise HTTPException(status_code=409, detail="Set a base voice before enabling auto calibration")

    project.auto_voice_calibrate = body.enabled
    project.updated_at = datetime.utcnow()
    session.add(project)
    await session.commit()

    return {"auto_voice_calibrate": body.enabled}


@router.post("/projects/{project_id}/shots/{shot_id}/voice-convert", status_code=202)
async def voice_convert_shot(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """Convert a shot's voice to match the reference voice."""
    from app.services.reference_voice import resolve_reference_prompt_wav

    project = await _get_project_or_404(project_id, session)

    if resolve_reference_prompt_wav(project_id, project) is None:
        raise HTTPException(status_code=400, detail="No reference voice set")

    if shot_id == project.reference_voice_shot_id:
        raise HTTPException(status_code=400, detail="Cannot convert the reference shot itself")

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

    shot.vc_status = "converting"
    shot.vc_error_message = None
    session.add(shot)
    await session.commit()

    arq = await _get_arq_redis(redis)
    await arq.enqueue_job(
        "run_voice_convert", project_id, shot_id, f"user:{user}",
        _queue_name="arq:vc",
    )

    return {"status": "queued", "shot_id": shot_id}


@router.post("/projects/{project_id}/voice-convert-all", status_code=202)
async def voice_convert_all(
    project_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """Convert all non-reference completed shots to match the reference voice."""
    from app.services.reference_voice import resolve_reference_prompt_wav

    project = await _get_project_or_404(project_id, session)

    if resolve_reference_prompt_wav(project_id, project) is None:
        raise HTTPException(status_code=400, detail="No reference voice set")

    # Find all completed shots except the reference shot (if a shot is the source)
    filters = [
        Shot.project_id == project_id,
        Shot.status == ShotStatus.COMPLETED.value,
    ]
    if project.reference_voice_shot_id is not None:
        filters.append(Shot.shot_id != project.reference_voice_shot_id)

    result = await session.execute(select(Shot).where(*filters))
    shots = result.scalars().all()

    if not shots:
        raise HTTPException(status_code=400, detail="No eligible shots to convert")

    shot_ids = []
    for shot in shots:
        shot.vc_status = "converting"
        shot.vc_error_message = None
        session.add(shot)
        shot_ids.append(shot.shot_id)
    await session.commit()

    arq = await _get_arq_redis(redis)
    await arq.enqueue_job(
        "run_voice_convert_batch", project_id, shot_ids, f"user:{user}",
        _queue_name="arq:vc",
    )

    return {"status": "queued", "shot_ids": shot_ids}


@router.post("/projects/{project_id}/shots/{shot_id}/voice-revert")
async def voice_revert_shot(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Revert a shot's voice conversion back to original audio."""
    await _get_project_or_404(project_id, session)

    result = await session.execute(
        select(Shot).where(
            Shot.project_id == project_id, Shot.shot_id == shot_id
        )
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    if shot.vc_status != "done":
        raise HTTPException(status_code=400, detail="Shot has not been voice-converted")

    # Restore pre-VC video
    pre_vc = shot_pre_vc_video_path(project_id, shot_id)
    if pre_vc.exists():
        video_path = Path(shot.video_path)
        shutil.copy2(str(pre_vc), str(video_path))

    shot.vc_status = None
    shot.vc_error_message = None
    session.add(shot)
    await session.commit()

    ts = int(datetime.utcnow().timestamp())
    return {
        "shot_id": shot_id,
        "vc_status": None,
        "video_path": to_media_url(shot.video_path),
        "version": ts,
    }
