"""裁剪后：新末帧在 COS、DB 存新 key、pre-CC 备份被清理、cc_status 重置。"""
from sqlalchemy import select

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import _make_project, _add_shot, seed_shot_with_source, HEADERS

from app.models.project import Shot
from app.services import object_store

pytestmark = requires_cos


async def test_trim_resets_cc_and_clears_pre_cc_object(
    client, db_session_factory, cos_prefix, tmp_path
):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await seed_shot_with_source(db_session_factory, pid, 1)

    # 造一个 pre-CC 备份对象并挂到 DB 上，模拟做过角色校准的分镜
    pre_cc_key = f"projects/{pid}/shots/shot_1/last_frame_pre_cc.png"
    f = tmp_path / "pre_cc.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    await object_store.put(pre_cc_key, f)
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.pre_cc_last_frame_key = pre_cc_key
        shot.cc_status = "done"
        await s.commit()

    r = await client.post(
        f"/api/projects/{pid}/shots/1/trim",
        json={"end_frame": 40}, headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()

    assert shot.cc_status is None
    assert shot.pre_cc_last_frame_key is None
    # DB 先解除引用，再删对象——此时对象应已不存在
    assert await object_store.exists(pre_cc_key) is False


async def test_trim_publishes_new_last_frame_and_leaves_source_untouched(
    client, db_session_factory, cos_prefix, tmp_path
):
    """裁剪只改元数据：video_path（源对象）字节不变；last_frame 重新抽取并发布新 key。"""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    source_key = await seed_shot_with_source(db_session_factory, pid, 1, frames=120)

    before = tmp_path / "before.mp4"
    await object_store.get(source_key, before)
    before_bytes = before.read_bytes()

    r = await client.post(
        f"/api/projects/{pid}/shots/1/trim",
        json={"end_frame": 40}, headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trim_frames"] == 40

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()

    # 源对象 key 与字节都不变（非破坏式：trim 只是元数据）
    assert shot.video_path == source_key
    after = tmp_path / "after.mp4"
    await object_store.get(source_key, after)
    assert after.read_bytes() == before_bytes, "源视频对象被修改——trim 必须是非破坏式的"

    # 新末帧真实发布到 COS
    assert shot.last_frame_path is not None
    assert shot.last_frame_path.startswith(f"projects/{pid}/shots/shot_1/last_frame_")
    assert await object_store.exists(shot.last_frame_path)


async def test_trim_below_min_frames_rejected(client, db_session_factory, cos_prefix):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await seed_shot_with_source(db_session_factory, pid, 1)

    r = await client.post(
        f"/api/projects/{pid}/shots/1/trim",
        json={"end_frame": 10}, headers=HEADERS,
    )
    assert r.status_code == 400


async def test_restore_trim_clears_metadata_and_republishes_last_frame(
    client, db_session_factory, cos_prefix
):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    source_key = await seed_shot_with_source(db_session_factory, pid, 1, frames=120)

    r = await client.post(
        f"/api/projects/{pid}/shots/1/trim",
        json={"end_frame": 40}, headers=HEADERS,
    )
    assert r.status_code == 200

    r = await client.post(
        f"/api/projects/{pid}/shots/1/restore-trim",
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["trim_frames"] is None

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
    assert shot.trim_frames is None
    assert shot.video_path == source_key
    assert await object_store.exists(shot.last_frame_path)
