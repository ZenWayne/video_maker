"""守卫：完整链路跑完后，storage_root 下不应留下任何持久文件。

这是「容器无状态」这一目标的可执行断言——若将来有人加了写本地的代码路径，
本测试会立刻失败。
"""
from pathlib import Path

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import _make_project, _add_shot, HEADERS
from tests.integration.conftest_cos_seed import seed_shot_source_to_oss

pytestmark = requires_cos

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 128


async def test_full_flow_leaves_storage_root_empty(client, db_session_factory, cos_prefix, tmp_path):
    """conftest 已把 settings.storage_root 指向 tmp_path。"""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await seed_shot_source_to_oss(db_session_factory, pid, 1, frames=30)

    r = await client.post(
        f"/api/projects/{pid}/reference-images",
        files={"files": ("face.png", PNG, "image/png")},
        data={"kind": "character"}, headers=HEADERS,
    )
    assert r.status_code in (200, 201), r.text

    r = await client.get(f"/api/projects/{pid}", headers=HEADERS)
    assert r.status_code == 200, r.text

    r = await client.post(
        f"/api/projects/{pid}/shots/1/trim",
        json={"end_frame": 24}, headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    leftovers = [p for p in Path(tmp_path).rglob("*") if p.is_file()]
    assert leftovers == [], f"仍在写本地磁盘：{leftovers}"
