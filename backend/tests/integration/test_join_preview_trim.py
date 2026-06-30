"""Regression: the continuity (join) preview must stitch the TRIMMED clips,
not the full source videos.

Real flow: seeds two shots with real videos, trims them via DB metadata, calls
the real /join-preview endpoint, then ffprobes the produced preview and asserts
its length equals the sum of the trimmed frame counts (not the full sources).
"""
import pytest
from sqlalchemy import select

from app.models.project import Shot
from app.services.storage import join_preview_path
from app.agents.video_trimmer import get_video_info
from tests.integration.conftest import HEADERS, _make_project, _add_shot, seed_shot_with_source


@pytest.mark.asyncio
async def test_join_preview_applies_trim(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="completed")
    await _add_shot(db_session_factory, pid, 2, status="completed")
    await seed_shot_with_source(db_session_factory, pid, 1, frames=120)
    await seed_shot_with_source(db_session_factory, pid, 2, frames=120)

    # trim via metadata: keep 50 + 40 frames
    async with db_session_factory() as s:
        for sid, n in ((1, 50), (2, 40)):
            shot = (await s.execute(
                select(Shot).where(Shot.project_id == pid, Shot.shot_id == sid)
            )).scalar_one()
            shot.trim_frames = n
        await s.commit()

    r = await client.post(
        f"/api/projects/{pid}/join-preview",
        json={"shot_ids": [1, 2]},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    # preview is written with a unique filename (join_preview_<ts_uuid>.mp4)
    previews = sorted(join_preview_path(pid).parent.glob("join_preview*.mp4"))
    assert previews, "no join preview produced"
    out = str(previews[-1])
    total = get_video_info(out)["total_frames"]
    # trimmed preview ≈ 50 + 40 = 90 (allow ±2 for concat re-encode rounding);
    # the bug stitched the full sources → ~240.
    assert 88 <= total <= 92, f"expected ~90 trimmed frames, got {total}"
    assert total < 200, f"preview used full untrimmed sources ({total} frames)"
