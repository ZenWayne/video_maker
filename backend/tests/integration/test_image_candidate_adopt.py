"""采纳候选：三种 slot 的写槽语义（复制而非移动；素材审计）."""
import pytest
from datetime import datetime
from pathlib import Path
from sqlalchemy import select

from tests.integration.conftest import HEADERS, _make_project, _add_shot
from app.models.project import ImageCandidate, Shot
from app.services.storage import shot_dir


async def _seed_done_candidate(sf, tmp_path_factory, pid, shot_id, slot, data=b"IMG"):
    f = tmp_path_factory / f"cand_{slot}.png"
    f.write_bytes(data)
    async with sf() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == shot_id)
        )).scalar_one()
        c = ImageCandidate(
            project_id=pid, shot_pk=shot.id, shot_id=shot_id, slot=slot,
            status="done", file_path=str(f),
        )
        s.add(c)
        await s.commit()
        await s.refresh(c)
        return c.id, f


async def _adopt(client, pid, shot_id, cid):
    return await client.post(
        f"/api/projects/{pid}/shots/{shot_id}/image-candidates/{cid}/adopt",
        headers=HEADERS,
    )


async def test_adopt_first_frame_copies_into_custom_frames(client, db_session_factory, tmp_path):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    cid, src = await _seed_done_candidate(db_session_factory, tmp_path, pid, 1, "first_frame")

    r = await _adopt(client, pid, 1, cid)
    assert r.status_code == 200

    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        assert shot.custom_first_frame_path is not None
        assert "custom_frames" in shot.custom_first_frame_path
        assert Path(shot.custom_first_frame_path).read_bytes() == b"IMG"
        assert shot.custom_first_frame_path != str(src)
    assert src.exists()  # 复制而非移动：候选原件不动


async def test_adopt_tail_frame_sets_path_and_tf_status(client, db_session_factory, tmp_path):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    cid, src = await _seed_done_candidate(db_session_factory, tmp_path, pid, 1, "tail_frame")

    r = await _adopt(client, pid, 1, cid)
    assert r.status_code == 200

    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        assert shot.target_last_frame_path is not None
        assert shot.tf_status == "done"
        assert Path(shot.target_last_frame_path).read_bytes() == b"IMG"
    assert src.exists()


async def test_adopt_cc_replaces_last_frame_and_propagates(client, db_session_factory, tmp_path):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await _add_shot(db_session_factory, pid, 2)

    # shot1 有 last_frame + 旧 cc 文件；shot2 未生成（可被连贯链更新）
    s_dir = shot_dir(pid, 1); s_dir.mkdir(parents=True, exist_ok=True)
    lf = s_dir / "last_frame_1_aaaa.png"; lf.write_bytes(b"OLD")
    old_cc = s_dir / "cc_0_bbbb.png"; old_cc.write_bytes(b"OLDCC")
    async with db_session_factory() as s:
        shot1 = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot1.last_frame_path = str(old_cc)
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
        p = Path(shot1.last_frame_path)
        assert p.name.startswith("cc_") and p.read_bytes() == b"NEWCC"
        assert not old_cc.exists()          # 旧校准帧被清
        assert lf.exists()                  # pristine 不动（revert 链保住）
        assert shot2.custom_first_frame_path == shot1.last_frame_path  # 连贯链传播


async def test_adopt_exclusive_per_slot_and_requires_done(client, db_session_factory, tmp_path):
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
