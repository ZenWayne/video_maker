"""导出合并落 COS；删除项目清空整个前缀。"""
from sqlalchemy import select

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import _make_project, _add_shot, seed_shot_with_source, HEADERS

from app.models.project import Project, Shot
from app.services import object_store
from app.services.storage import project_prefix

pytestmark = requires_cos


async def test_merge_publishes_final_video(db_session_factory, cos_prefix):
    from worker.tasks import merge_project_shots

    pid = await _make_project(db_session_factory, status="shot_review")
    for i in (1, 2):
        await _add_shot(db_session_factory, pid, i)
        await seed_shot_with_source(db_session_factory, pid, i, frames=30)

    key = await merge_project_shots(db_session_factory, pid)

    assert key == f"projects/{pid}/final/merged.mp4"
    assert await object_store.exists(key)
    assert await object_store.size(key) > 0

    async with db_session_factory() as s:
        proj = (await s.execute(select(Project).where(Project.id == pid))).scalar_one()
    assert proj.final_video_path == key


async def test_merge_bakes_trim_and_skips_incomplete_shots(db_session_factory, cos_prefix):
    """Regression: export must apply the non-destructive EDL (trim), and must
    only include COMPLETED shots — not pending/failed ones."""
    from worker.tasks import merge_project_shots
    from app.agents.video_trimmer import get_video_info

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="completed")
    await seed_shot_with_source(db_session_factory, pid, 1, frames=90)
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.trim_frames = 30
        await s.commit()
    # a pending shot with no video must NOT block or get included in the merge
    await _add_shot(db_session_factory, pid, 2, status="pending")

    key = await merge_project_shots(db_session_factory, pid)

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        local = Path(td) / "merged.mp4"
        await object_store.get(key, local)
        total = get_video_info(str(local))["total_frames"]
    # single completed+trimmed shot, single-input merge is a straight copy of
    # the baked (trimmed) clip -> ~30 frames, not the full 90.
    assert 28 <= total <= 32, f"expected ~30 trimmed frames, got {total}"


async def test_delete_project_clears_cos_prefix(client, db_session_factory, cos_prefix):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await seed_shot_with_source(db_session_factory, pid, 1, frames=30)

    assert len(await object_store.list_prefix(project_prefix(pid))) > 0

    r = await client.delete(f"/api/projects/{pid}", headers=HEADERS)
    assert r.status_code in (200, 204)

    assert await object_store.list_prefix(project_prefix(pid)) == []
