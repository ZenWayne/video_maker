"""候选创建/删除端点（ARQ mock、真实 in-memory DB）."""
import io
import json
import pytest
from sqlalchemy import select

from tests.integration.conftest import HEADERS, USER, _make_project, _add_shot, _add_character_image
from app.models.project import ImageCandidate, ReferenceImage


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


async def test_create_custom_candidate_with_temp_upload(client, db_session_factory, tmp_path):
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
        assert len(refs["object"]) == 1
        assert "candidates" in refs["object"][0]  # 临时上传进 candidates 目录
        from pathlib import Path
        assert Path(refs["object"][0]).read_bytes() == b"\x89PNGx"
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


async def test_delete_candidate(client, db_session_factory, tmp_path):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    r = await _create(client, pid)
    cid = r.json()["candidate"]["id"]

    # generating → 409
    r2 = await client.delete(
        f"/api/projects/{pid}/shots/1/image-candidates/{cid}", headers=HEADERS
    )
    assert r2.status_code == 409

    # done + 有文件 → 删行 + unlink
    f = tmp_path / "c.png"; f.write_bytes(b"x")
    async with db_session_factory() as s:
        row = (await s.execute(select(ImageCandidate).where(ImageCandidate.id == cid))).scalar_one()
        row.status = "done"; row.file_path = str(f)
        await s.commit()
    r3 = await client.delete(
        f"/api/projects/{pid}/shots/1/image-candidates/{cid}", headers=HEADERS
    )
    assert r3.status_code == 200
    assert not f.exists()
    async with db_session_factory() as s:
        assert (await s.execute(select(ImageCandidate))).scalar_one_or_none() is None
