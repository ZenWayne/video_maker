"""Auto voice-calibration trigger fired when a shot finishes video generation."""
import logging
from arq.connections import ArqRedis  # same import the API uses (app/api/pipeline.py:12)

from app.services.reference_voice import resolve_reference_prompt_wav

logger = logging.getLogger("worker")


async def maybe_enqueue_auto_vc(redis, session, project_id, project, shot) -> bool:
    """Enqueue voice conversion for a freshly completed shot if auto-calibrate is on.

    Returns True if a job was enqueued. Honors mutual exclusivity (file or shot
    source), skips the reference shot itself, and skips shots already in/through VC.
    """
    if not getattr(project, "auto_voice_calibrate", False):
        return False
    if resolve_reference_prompt_wav(project_id, project) is None:
        return False
    if shot.shot_id == project.reference_voice_shot_id:
        return False
    if shot.vc_status is not None:
        return False

    arq = ArqRedis(redis.connection_pool)
    await arq.enqueue_job(
        "run_voice_convert", project_id, shot.shot_id, "system:auto-vc",
        _queue_name="arq:vc",
    )
    shot.vc_status = "converting"
    session.add(shot)
    await session.commit()
    logger.info("Auto VC enqueued for project %s shot %d", project_id, shot.shot_id)
    return True
