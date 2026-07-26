"""候选创建/删除端点（ARQ mock、真实 in-memory DB）。文件上传/删除会真的打 COS
——涉及的用例用 requires_cos 单独 gate，其余（不碰文件的路由/校验逻辑）不需要。"""
import io
import json
from datetime import datetime, timedelta
import pytest
from sqlalchemy import select

from tests.integration.conftest import HEADERS, USER, _make_project, _add_shot, _add_character_image
from tests.integration.conftest_cos import requires_cos
from app.models.project import ImageCandidate, ReferenceImage
from app.services import object_store


async def _create(client, pid, shot_id=1, data=None, files=None):
    return await client.post(
        f"/api/projects/{pid}/shots/{shot_id}/image-candidates",
        data=data or {"slot": "tail_frame"},
        files=files,
        headers=HEADERS,
    )


async def test_create_auto_candidate_enqueues_worker(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)

    r = await _create(client, pid)
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    cand = body["candidate"]
    assert cand["slot"] == "tail_frame"
    assert cand["status"] == "generating"
    assert cand["prompt_source"] == "auto"

    client.arq.enqueue_job.assert_called_once_with(
        "run_image_candidate", pid, 1, cand["id"], f"user:{USER}"
    )


@requires_cos
async def test_create_custom_candidate_with_temp_upload(
    client, db_session_factory, tmp_path, cos_prefix
):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)

    r = await _create(
        client, pid,
        data={"slot": "first_frame", "custom_prompt": "少女转身", "include_shot_refs": "false"},
        files=[("files", ("extra.png", io.BytesIO(b"\x89PNGx"), "image/png"))],
    )
    assert r.status_code == 202
    cand = r.json()["candidate"]
    assert cand["prompt_source"] == "custom"
    assert cand["custom_prompt"] == "少女转身"

    async with db_session_factory() as s:
        row = (await s.execute(select(ImageCandidate))).scalar_one()
        refs = json.loads(row.ref_paths)
        assert "character" not in refs  # 未显式传 ref_image_ids → 键不写入，worker 端回退默认角色参考图
        assert len(refs["object"]) == 1
        assert "candidates/" in refs["object"][0]  # 临时上传进 candidates/ 前缀
        got = tmp_path / "got.png"
        await object_store.get(refs["object"][0], got)
        assert got.read_bytes() == b"\x89PNGx"
        # 未产生 ReferenceImage 行
        assert (await s.execute(select(ReferenceImage))).scalar_one_or_none() is None


async def test_create_resolves_ref_image_ids_by_kind(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    img_id = await _add_character_image(db_session_factory, pid)

    r = await _create(client, pid, data={
        "slot": "tail_frame",
        "ref_image_ids": json.dumps([img_id]),
    })
    assert r.status_code == 202
    async with db_session_factory() as s:
        row = (await s.execute(select(ImageCandidate))).scalar_one()
        refs = json.loads(row.ref_paths)
        assert refs["character"] == [f"/fake/{pid}/test.jpg"]


async def test_create_rejects_bad_slot(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    r = await _create(client, pid, data={"slot": "cc"})
    assert r.status_code == 400  # cc 候选只能由校准端点产生


async def test_create_rejects_malformed_ref_image_ids(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    # not JSON at all
    r1 = await _create(client, pid, data={"slot": "tail_frame", "ref_image_ids": "{not json"})
    assert r1.status_code == 400
    # valid JSON but not an array
    r2 = await _create(client, pid, data={"slot": "tail_frame", "ref_image_ids": "5"})
    assert r2.status_code == 400


@requires_cos
async def test_delete_candidate(client, db_session_factory, tmp_path, cos_prefix):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    r = await _create(client, pid)
    cid = r.json()["candidate"]["id"]

    # generating → 409
    r2 = await client.delete(
        f"/api/projects/{pid}/shots/1/image-candidates/{cid}", headers=HEADERS
    )
    assert r2.status_code == 409

    # done + 有文件 → 删行 + 删 COS 对象
    key = f"projects/{pid}/shots/shot_1/candidates/c.png"
    f = tmp_path / "c.png"; f.write_bytes(b"x")
    await object_store.put(key, f)
    async with db_session_factory() as s:
        row = (await s.execute(select(ImageCandidate).where(ImageCandidate.id == cid))).scalar_one()
        row.status = "done"; row.file_path = key
        await s.commit()
    r3 = await client.delete(
        f"/api/projects/{pid}/shots/1/image-candidates/{cid}", headers=HEADERS
    )
    assert r3.status_code == 200
    assert not await object_store.exists(key)
    async with db_session_factory() as s:
        assert (await s.execute(select(ImageCandidate))).scalar_one_or_none() is None


async def test_delete_stuck_generating_candidate_after_timeout(client, db_session_factory):
    """worker 挂掉后卡在 generating 超过 30 分钟的候选应可删除。"""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    r = await _create(client, pid)
    cid = r.json()["candidate"]["id"]

    async with db_session_factory() as s:
        row = (await s.execute(select(ImageCandidate).where(ImageCandidate.id == cid))).scalar_one()
        row.created_at = datetime.utcnow() - timedelta(minutes=31)
        await s.commit()

    r2 = await client.delete(
        f"/api/projects/{pid}/shots/1/image-candidates/{cid}", headers=HEADERS
    )
    assert r2.status_code == 200
    async with db_session_factory() as s:
        assert (await s.execute(select(ImageCandidate))).scalar_one_or_none() is None
