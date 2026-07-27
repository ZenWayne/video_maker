"""Workspace.path 的路径逃逸防护——纯 pathlib 逻辑，不碰 COS。

不能放进 tests/integration/test_workspace.py：那个模块整体标了
``pytestmark = requires_cos``，这几条用例会被一起跳过，导致无凭证环境里
完全测不到"绝不允许写到工作区外面"这条 Important 级守卫——path()/fetch()
对 name 参数不做校验的话，绝对路径或 .. 会让 pathlib 丢弃 self.root 这个
左操作数，写出的文件不会被退出时的 rmtree 清掉，击穿本地不留持久文件的
设计前提。
"""
import pytest

from app.services.workspace import workspace


async def test_path_rejects_absolute_name():
    async with workspace() as ws:
        with pytest.raises(ValueError):
            ws.path("/etc/passwd")


async def test_path_rejects_dotdot_name():
    async with workspace() as ws:
        with pytest.raises(ValueError):
            ws.path("../escape.txt")


async def test_path_accepts_nested_relative_name():
    async with workspace() as ws:
        p = ws.path("sub/dir/out.png")
        assert p == ws.root / "sub" / "dir" / "out.png"
        assert p.parent.is_dir()
