"""workspace() 的临时目录清理——纯本地 tempfile/shutil 逻辑，不碰 COS。

自查发现：该用例原在 tests/integration/test_workspace.py 里，带着一个从未
使用的 cos_prefix 形参，被模块级 pytestmark = requires_cos 一起挡住，导致
无凭证环境测不到"异常路径下 tmpdir 也必须被删除"这条守卫本项目无状态方案
的核心用例——与 ensure_free_space / Workspace.path 撞见的是同一类缺陷。
"""
import pytest

from app.services.workspace import workspace


async def test_tmpdir_removed_even_on_exception():
    captured = {}
    with pytest.raises(ValueError):
        async with workspace() as ws:
            captured["root"] = ws.root
            raise ValueError("boom")
    assert not captured["root"].exists()
