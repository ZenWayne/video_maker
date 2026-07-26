"""上传链路：文件落 COS，DB 存 key；JSON 数组字段每项都是 key。

覆盖 app/api/uploads.py（项目级角色/场景参考图）与 app/api/pipeline.py 的
上传/拷贝/删除端点（分镜自定义参考图、首帧/尾帧上传与提取）。
"""
import json

from sqlalchemy import select

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import _make_project, _add_shot, HEADERS

from app.models.project import ReferenceImage, Shot
from app.services import object_store

pytestmark = requires_cos

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 128


async def test_reference_image_upload_lands_in_oss(client, db_session_factory, cos_prefix):
    pid = await _make_project(db_session_factory, status="draft")

    r = await client.post(
        f"/api/projects/{pid}/reference-images",
        files={"files": ("face.png", PNG, "image/png")},
        data={"kind": "character"},
        headers=HEADERS,
    )
    assert r.status_code in (200, 201)

    async with db_session_factory() as s:
        img = (await s.execute(
            select(ReferenceImage).where(ReferenceImage.project_id == pid)
        )).scalars().first()

    assert img.storage_path.startswith(f"projects/{pid}/reference_images/")
    assert not img.storage_path.startswith("/")
    assert await object_store.exists(img.storage_path)


async def test_delete_reference_image_removes_from_oss(client, db_session_factory, cos_prefix):
    pid = await _make_project(db_session_factory, status="draft")
    r = await client.post(
        f"/api/projects/{pid}/reference-images",
        files={"files": ("face.png", PNG, "image/png")},
        data={"kind": "character"},
        headers=HEADERS,
    )
    img_id = r.json()[0]["id"]
    key = r.json()[0]["storage_path"]
    assert await object_store.exists(key)

    r2 = await client.delete(f"/api/projects/{pid}/reference-images/{img_id}", headers=HEADERS)
    assert r2.status_code == 204
    assert not await object_store.exists(key)


async def test_custom_reference_paths_stores_keys_not_paths(client, db_session_factory, cos_prefix):
    """JSON 数组字段最容易漏——数组内每一项都必须是 key。"""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)

    r = await client.post(
        f"/api/projects/{pid}/shots/1/reference-images",
        files=[
            ("files", ("a.png", PNG, "image/png")),
            ("files", ("b.png", PNG, "image/png")),
        ],
        headers=HEADERS,
    )
    assert r.status_code == 200

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()

    keys = json.loads(shot.custom_reference_paths)
    assert len(keys) == 2
    for k in keys:
        assert k.startswith(f"projects/{pid}/shots/shot_1/custom_frames/")
        assert not k.startswith("/")
        assert await object_store.exists(k)


async def test_delete_single_reference_image_removes_from_oss(client, db_session_factory, cos_prefix):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await client.post(
        f"/api/projects/{pid}/shots/1/reference-images",
        files=[
            ("files", ("a.png", PNG, "image/png")),
            ("files", ("b.png", PNG, "image/png")),
        ],
        headers=HEADERS,
    )
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
    keys = json.loads(shot.custom_reference_paths)
    assert len(keys) == 2

    r = await client.delete(
        f"/api/projects/{pid}/shots/1/reference-images", params={"index": 0}, headers=HEADERS
    )
    assert r.status_code == 200
    assert not await object_store.exists(keys[0])
    assert await object_store.exists(keys[1])

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
    remaining = json.loads(shot.custom_reference_paths)
    assert remaining == [keys[1]]


async def test_delete_all_reference_images_clears_prefix(client, db_session_factory, cos_prefix):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await client.post(
        f"/api/projects/{pid}/shots/1/reference-images",
        files=[("files", ("a.png", PNG, "image/png"))],
        headers=HEADERS,
    )
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
    key = json.loads(shot.custom_reference_paths)[0]

    r = await client.delete(f"/api/projects/{pid}/shots/1/reference-images", headers=HEADERS)
    assert r.status_code == 200
    assert not await object_store.exists(key)

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
    assert shot.custom_reference_paths is None


async def test_upload_first_frame_lands_in_oss(client, db_session_factory, cos_prefix):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)

    r = await client.post(
        f"/api/projects/{pid}/shots/1/upload-first-frame",
        files={"file": ("ff.png", PNG, "image/png")},
        headers=HEADERS,
    )
    assert r.status_code == 200

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
    key = shot.custom_first_frame_path
    assert key.startswith(f"projects/{pid}/shots/shot_1/custom_frames/")
    assert await object_store.exists(key)


async def test_upload_tail_frame_lands_in_oss(client, db_session_factory, cos_prefix):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)

    r = await client.post(
        f"/api/projects/{pid}/shots/1/upload-tail-frame",
        files={"file": ("tf.png", PNG, "image/png")},
        headers=HEADERS,
    )
    assert r.status_code == 200

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
    key = shot.target_last_frame_path
    assert key.startswith(f"projects/{pid}/shots/shot_1/")
    assert "custom_frames" not in key
    assert shot.tf_status == "done"
    assert await object_store.exists(key)


async def test_delete_tail_frame_removes_from_oss(client, db_session_factory, cos_prefix):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await client.post(
        f"/api/projects/{pid}/shots/1/upload-tail-frame",
        files={"file": ("tf.png", PNG, "image/png")},
        headers=HEADERS,
    )
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
    key = shot.target_last_frame_path

    r = await client.post(f"/api/projects/{pid}/shots/1/delete-tail-frame", headers=HEADERS)
    assert r.status_code == 200
    assert not await object_store.exists(key)


async def test_delete_first_frame_removes_from_oss(client, db_session_factory, cos_prefix):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await client.post(
        f"/api/projects/{pid}/shots/1/upload-first-frame",
        files={"file": ("ff.png", PNG, "image/png")},
        headers=HEADERS,
    )
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.ff_status = "failed"
        shot.ff_error_message = "boom"
        await s.commit()
    key = shot.custom_first_frame_path

    r = await client.delete(f"/api/projects/{pid}/shots/1/first-frame", headers=HEADERS)
    assert r.status_code == 200
    assert not await object_store.exists(key)

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
    assert shot.custom_first_frame_path is None
    assert shot.ff_status is None


async def test_extract_last_frame_400_when_key_absent(client, db_session_factory, cos_prefix):
    """400 when last_frame_path is set but the COS object doesn't exist."""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    ghost_key = f"projects/{pid}/shots/shot_1/ghost_last_frame.png"  # never put to COS
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.last_frame_path = ghost_key
        await s.commit()

    r = await client.post(f"/api/projects/{pid}/shots/1/extract-last-frame", headers=HEADERS)
    assert r.status_code == 400


async def test_extract_last_frame_copies_to_new_key(client, db_session_factory, cos_prefix, tmp_path):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    lf_key = f"projects/{pid}/shots/shot_1/last_frame_1_aaaa.png"
    f = tmp_path / "lf.png"
    f.write_bytes(PNG)
    await object_store.put(lf_key, f)
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.last_frame_path = lf_key
        await s.commit()

    r = await client.post(f"/api/projects/{pid}/shots/1/extract-last-frame", headers=HEADERS)
    assert r.status_code == 200

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
    dest_key = shot.target_last_frame_path
    assert dest_key != lf_key
    assert dest_key.startswith(f"projects/{pid}/shots/shot_1/")
    assert await object_store.exists(dest_key)
    assert await object_store.exists(lf_key)  # 源文件不受影响（copy 非 move）


async def test_extract_tail_frame_copies_last_frame_to_target(client, db_session_factory, cos_prefix, tmp_path):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    lf_key = f"projects/{pid}/shots/shot_1/last_frame_1_aaaa.png"
    f = tmp_path / "lf.png"
    f.write_bytes(PNG)
    await object_store.put(lf_key, f)
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.last_frame_path = lf_key
        await s.commit()

    r = await client.post(f"/api/projects/{pid}/shots/1/extract-tail-frame", headers=HEADERS)
    assert r.status_code == 200

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
    dest_key = shot.target_last_frame_path
    assert dest_key != lf_key
    assert dest_key.startswith(f"projects/{pid}/shots/shot_1/")
    assert shot.tf_status == "done"
    assert await object_store.exists(dest_key)
    assert await object_store.exists(lf_key)


async def test_use_prev_last_frame_copies_to_new_key(client, db_session_factory, cos_prefix, tmp_path):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await _add_shot(db_session_factory, pid, 2)
    lf_key = f"projects/{pid}/shots/shot_1/last_frame_1_aaaa.png"
    f = tmp_path / "lf.png"
    f.write_bytes(PNG)
    await object_store.put(lf_key, f)
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.last_frame_path = lf_key
        await s.commit()

    r = await client.post(f"/api/projects/{pid}/shots/2/use-prev-last-frame", headers=HEADERS)
    assert r.status_code == 200

    async with db_session_factory() as s:
        shot2 = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 2)
        )).scalar_one()
    dest_key = shot2.custom_first_frame_path
    assert dest_key.startswith(f"projects/{pid}/shots/shot_2/custom_frames/")
    assert await object_store.exists(dest_key)
