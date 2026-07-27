"""API 返回的媒体 URL 必须能被浏览器直接 GET 到真实内容。"""
import httpx

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import _make_project, _add_shot, HEADERS
from tests.integration.conftest_cos_seed import seed_shot_source_to_oss

# 注：不用文件级 pytestmark——test_media_static_mount_is_gone 和
# test_null_media_fields_stay_null 都不碰 COS，只有真正 seed 真实视频的用例才
# 需要 @requires_cos + cos_prefix。test_media_static_mount_is_gone 尤其重要：
# 它是「/api/media 绕过签名的后门已关闭」这个安全交付物的唯一回归测试，绝不能
# 被误 gate 到无凭证环境里默默 skip 掉（审查发现的过度 gate 问题）。


@requires_cos
async def test_project_response_video_url_is_fetchable(client, db_session_factory, cos_prefix):
    # NOTE: cos_prefix is required (not just cosmetic) — it's what warms COS
    # credentials for this test process; without it, seed_shot_source_to_oss's
    # real object_store.put() raises "COS 凭证缓存为空" (see constraint on
    # requires_cos/cos_prefix gating in the Task 12 brief).
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await seed_shot_source_to_oss(db_session_factory, pid, 1, frames=30)

    r = await client.get(f"/api/projects/{pid}", headers=HEADERS)
    assert r.status_code == 200
    url = r.json()["shots"][0]["video_path"]

    assert url.startswith("http")
    assert "/api/media/" not in url

    async with httpx.AsyncClient(timeout=30) as c:
        head = await c.get(url, headers={"Range": "bytes=0-99"})
    assert head.status_code in (200, 206)
    assert len(head.content) > 0


async def test_media_static_mount_is_gone(client):
    """/api/media 静态挂载必须已删除，否则等于留了一条绕过签名的后门。"""
    r = await client.get("/api/media/projects/anything.mp4")
    assert r.status_code == 404


async def test_null_media_fields_stay_null(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)

    r = await client.get(f"/api/projects/{pid}", headers=HEADERS)
    shot = r.json()["shots"][0]
    assert shot["video_path"] is None
    assert shot["last_frame_path"] is None
