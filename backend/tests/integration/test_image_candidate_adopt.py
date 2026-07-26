"""采纳候选：三种 slot 的写槽语义（服务端 copy 而非移动；素材审计）。"""
import pytest
from datetime import datetime
from sqlalchemy import select

from tests.integration.conftest import HEADERS, _make_project, _add_shot
from tests.integration.conftest_cos import requires_cos
from app.models.project import ImageCandidate, Shot
from app.services import object_store

pytestmark = requires_cos


async def _seed_done_candidate(sf, tmp_path, pid, shot_id, slot, data=b"IMG"):
    key = f"projects/{pid}/shots/shot_{shot_id}/candidates/cand_{slot}.png"
    f = tmp_path / f"cand_{slot}.png"
    f.write_bytes(data)
    await object_store.put(key, f)
    async with sf() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == shot_id)
        )).scalar_one()
        c = ImageCandidate(
            project_id=pid, shot_pk=shot.id, shot_id=shot_id, slot=slot,
            status="done", file_path=key,
        )
        s.add(c)
        await s.commit()
        await s.refresh(c)
        return c.id, key


async def _adopt(client, pid, shot_id, cid):
    return await client.post(
        f"/api/projects/{pid}/shots/{shot_id}/image-candidates/{cid}/adopt",
        headers=HEADERS,
    )


async def test_adopt_first_frame_copies_into_custom_frames(
    client, db_session_factory, tmp_path, cos_prefix
):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    cid, src_key = await _seed_done_candidate(db_session_factory, tmp_path, pid, 1, "first_frame")

    r = await _adopt(client, pid, 1, cid)
    assert r.status_code == 200

    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        assert shot.custom_first_frame_path is not None
        assert "custom_frames" in shot.custom_first_frame_path
        got = tmp_path / "got.png"
        await object_store.get(shot.custom_first_frame_path, got)
        assert got.read_bytes() == b"IMG"
        assert shot.custom_first_frame_path != src_key
    assert await object_store.exists(src_key)  # 复制而非移动：候选原件不动


async def test_adopt_tail_frame_sets_path_and_tf_status(
    client, db_session_factory, tmp_path, cos_prefix
):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    cid, src_key = await _seed_done_candidate(db_session_factory, tmp_path, pid, 1, "tail_frame")

    r = await _adopt(client, pid, 1, cid)
    assert r.status_code == 200

    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        assert shot.target_last_frame_path is not None
        assert shot.tf_status == "done"
        got = tmp_path / "got.png"
        await object_store.get(shot.target_last_frame_path, got)
        assert got.read_bytes() == b"IMG"
    assert await object_store.exists(src_key)


async def test_adopt_cc_replaces_last_frame_and_propagates(
    client, db_session_factory, tmp_path, cos_prefix
):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await _add_shot(db_session_factory, pid, 2)

    # shot1 有 pristine last_frame + 旧 cc 文件（当前展示的是旧校准帧）；
    # shot2 未生成（可被连贯链更新）
    pristine_key = f"projects/{pid}/shots/shot_1/last_frame_pristine.png"
    lf = tmp_path / "pristine.png"; lf.write_bytes(b"OLD")
    await object_store.put(pristine_key, lf)

    old_cc_key = f"projects/{pid}/shots/shot_1/cc_old.png"
    old_cc = tmp_path / "old_cc.png"; old_cc.write_bytes(b"OLDCC")
    await object_store.put(old_cc_key, old_cc)

    async with db_session_factory() as s:
        shot1 = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot1.last_frame_path = old_cc_key
        shot1.pristine_last_frame_key = pristine_key
        await s.commit()

    cid, _ = await _seed_done_candidate(db_session_factory, tmp_path, pid, 1, "cc", data=b"NEWCC")
    r = await _adopt(client, pid, 1, cid)
    assert r.status_code == 200

    async with db_session_factory() as s:
        shot1 = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot2 = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 2)
        )).scalar_one()
        assert shot1.cc_status == "done"
        assert shot1.last_frame_path.split("/")[-1].startswith("cc_")
        got = tmp_path / "got_new.png"
        await object_store.get(shot1.last_frame_path, got)
        assert got.read_bytes() == b"NEWCC"
        assert not await object_store.exists(old_cc_key)   # 旧校准帧被清
        assert shot1.pristine_last_frame_key == pristine_key  # pristine 不动（revert 链保住）
        assert await object_store.exists(pristine_key)
        assert shot2.custom_first_frame_path == shot1.last_frame_path  # 连贯链传播


async def test_adopt_exclusive_per_slot_and_requires_done(
    client, db_session_factory, tmp_path, cos_prefix
):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    cid1, _ = await _seed_done_candidate(db_session_factory, tmp_path, pid, 1, "tail_frame", b"A")
    cid2, _ = await _seed_done_candidate(db_session_factory, tmp_path, pid, 1, "tail_frame", b"B")

    assert (await _adopt(client, pid, 1, cid1)).status_code == 200
    assert (await _adopt(client, pid, 1, cid2)).status_code == 200

    async with db_session_factory() as s:
        rows = (await s.execute(select(ImageCandidate))).scalars().all()
        adopted = {c.id: c.adopted_at for c in rows}
        assert adopted[cid2] is not None and adopted[cid1] is None  # 同槽位互斥

    # 未完成候选不可采纳
    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        c = ImageCandidate(project_id=pid, shot_pk=shot.id, shot_id=1, slot="tail_frame")
        s.add(c); await s.commit(); await s.refresh(c)
        pending = c.id
    assert (await _adopt(client, pid, 1, pending)).status_code == 400
