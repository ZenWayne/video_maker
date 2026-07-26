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
from app.services.storage import final_video_key, reference_images_prefix


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


@requires_cos
async def test_serve_asset_reference_image_redirects_to_fetchable_content(
    client, db_session_factory, tmp_path, cos_prefix
):
    """serve_asset 的正向覆盖：目前只有 download_final 有真实 302->可下载内容
    的验证，serve_asset 反而是三分支、逻辑更复杂的那个路由，之前完全没有
    正向测试。种一个真实 reference_images 对象，走 reference_images 分支，
    跟随 302 真的 GET 到内容——不只断言 URL 字符串形态。
    """
    pid = await _make_project(db_session_factory, status="shot_review")

    f = tmp_path / "ref.png"
    f.write_bytes(b"fake reference image bytes")
    key = f"{reference_images_prefix(pid)}ref.png"
    await object_store.put(key, f)

    r = await client.get(
        f"/api/projects/{pid}/assets/reference_images/ref.png",
        headers=HEADERS, follow_redirects=False,
    )
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("http")

    async with httpx.AsyncClient(timeout=30) as c:
        got = await c.get(loc)
    assert got.status_code == 200
    assert got.content == b"fake reference image bytes"


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
    """路径穿越必须在 is_valid_key() 这层被拒，而不是靠路由匹配"碰巧"挡住。

    ``%2F``（编码斜杠）在 ASGI 层会被解成真正的 "/"，导致请求在到达
    serve_asset 之前就因为路径段数对不上而被 Starlette 路由匹配拒掉——那时
    拿到的是框架的通用 404（``{"detail":"Not Found"}``），跟 is_valid_key()
    毫无关系，是一次假阳性覆盖。single-segment 的 ``%2e%2e``（即 ".."）不含
    斜杠，能正常路由进 serve_asset，Path(file).name 不会剥掉它（Path("..").name
    == ".."），key 拼出来后必须在 is_valid_key() 里因为含 ".." 分量被拒——
    这里断言的是 handler 自己返回的 400 + "Invalid key"，用来跟框架 404 区分开。
    """
    pid = await _make_project(db_session_factory, status="shot_review")
    r = await client.get(
        f"/api/projects/{pid}/assets/reference_images/%2e%2e",
        headers=HEADERS, follow_redirects=False,
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "Invalid key"


async def test_download_final_rejects_traversal_via_project_id(client):
    """download_final 没有 file/kind 参数，唯一的用户可控输入是 project_id——
    同样必须走到 is_valid_key() 而不是被路由挡在外面（download_final 之前
    完全没调 is_valid_key，这个用例专门盯这条路径不回归）。
    """
    r = await client.get(
        "/api/projects/%2e%2e/final.mp4",
        headers=HEADERS, follow_redirects=False,
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "Invalid key"
