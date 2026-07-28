"""storyboard.json COS I/O: write, archive, read.

Shared by worker.tasks (write on screenwriter completion) and app.api.pipeline /
app.api.projects (write on edit, archive on regenerate/reset, read for response
serialization) so all three stay in lockstep on the key format and the
write-then-DB-field-update ordering.

Depends only on app.services.* — safe for worker to import (never import
app.api.* from worker: the vc-worker process only imports worker.tasks and
never app.main, so an app.api.* import pulled in at module load time can hit
a circular-import ImportError the first time that worker handles a job; see
CLAUDE.md's "Shot 素材文件变更审计" section).
"""

import json
from typing import Optional

from app.services import object_store
from app.services.storage import archived_storyboard_key, storyboard_key
from app.services.workspace import workspace


async def write_storyboard(project_id: str, scene_overview, shots: list[dict]) -> str:
    """Stage storyboard.json in a one-shot workspace and publish it.

    Returns the key (always ``storyboard_key(project_id)`` — the live
    storyboard has one canonical key; prior versions live under
    ``archived_storyboard_key``, see ``archive_storyboard``).
    """
    async with workspace() as ws:
        local = ws.path("storyboard.json")
        local.write_text(
            json.dumps(
                {"scene_overview": scene_overview, "shots": shots},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return await ws.publish(local, storyboard_key(project_id))


async def archive_storyboard(project_id: str, timestamp: str) -> None:
    """Copy the live storyboard.json to an archived key, then delete the live one.

    No-op when there is no live storyboard yet — nothing to archive (e.g. a
    project reset before scripting ever produced one).
    """
    key = storyboard_key(project_id)
    if not await object_store.exists(key):
        return
    await object_store.copy(key, archived_storyboard_key(project_id, timestamp))
    await object_store.delete(key)


async def read_storyboard(key: Optional[str]) -> Optional[dict]:
    """Fetch and parse the storyboard.json at `key`.

    Returns None when `key` is falsy or the object is missing/unreadable —
    mirrors the previous local-path behavior, where a missing/corrupt file
    silently degraded the response's storyboard field instead of raising.
    """
    if not key:
        return None
    try:
        async with workspace() as ws:
            local = await ws.fetch(key, name="storyboard.json")
            return json.loads(local.read_text(encoding="utf-8"))
    except Exception:
        return None
