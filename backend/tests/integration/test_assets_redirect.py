"""assets 路由改 302 重定向到签名 URL。

按项目 gating 约定（conftest_cos.py + test_cos_gating_hygiene.py）：只有真正
打真实 COS 网络的用例才带 cos_prefix 参数 + @requires_cos；404 与路径穿越
两个用例都只走本地/mock 分支，不带 cos_prefix，因此不加 @requires_cos，必须
留在无凭证环境的常规回归里跑。不用文件级 pytestmark。
"""
from unittest.mock import AsyncMock

import httpx

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import _make_project, HEADERS

from app.services import object_store
from app.services.storage import final_video_key


@requires_cos
async def test_final_mp4_redirects_with_attachment_header(
    client, db_session_factory, tmp_path, cos_prefix
):
    """真实 COS 端到端：302 重定向真的能取到内容，且附件头由 COS 直接返回。

    final_video_key(pid) 由 project_id 派生，无法落进 cos_prefix 隔离的前缀
    下；cos_prefix 这里只用于预热本进程的 COS 凭证（与
    test_cos_media_url.py::test_project_response_video_url_is_fetchable 的
    注释一致），真实对象会成为孤儿留在 bucket 里——符合 object_store.py
    「宁可留孤儿对象」的一致性约定。
    """
    pid = await _make_project(db_session_factory, status="shot_review")

    f = tmp_path / "merged.mp4"
    f.write_bytes(b"fake merged video")
    await object_store.put(final_video_key(pid), f)

    r = await client.get(
        f"/api/projects/{pid}/final.mp4", headers=HEADERS, follow_redirects=False
    )
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("http")

    async with httpx.AsyncClient(timeout=30) as c:
        got = await c.get(loc)
    assert got.status_code == 200
    assert got.content == b"fake merged video"
    # 由 COS 直接返回附件下载头，后端不中转流量
    assert "merged.mp4" in got.headers.get("content-disposition", "")


async def test_final_mp4_404_when_absent(client, db_session_factory, monkeypatch):
    from app.api import assets as assets_module

    monkeypatch.setattr(
        assets_module.object_store, "exists", AsyncMock(return_value=False)
    )

    pid = await _make_project(db_session_factory, status="shot_review")
    r = await client.get(
        f"/api/projects/{pid}/final.mp4", headers=HEADERS, follow_redirects=False
    )
    assert r.status_code == 404


async def test_asset_route_rejects_traversal(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    r = await client.get(
        f"/api/projects/{pid}/assets/reference_images/..%2F..%2Fsecret.txt",
        headers=HEADERS, follow_redirects=False,
    )
    assert r.status_code in (400, 404)
