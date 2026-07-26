"""workspace.ensure_free_space —— 纯本地 shutil.disk_usage 检查，不碰 COS。

不能放进 tests/integration/test_workspace.py：那个模块整体标了
``pytestmark = requires_cos``，这条用例会被一起跳过，导致无凭证环境里
完全测不到这段磁盘空间预检逻辑（它是 Task 11 导出合并的地基——不预检的
表现是 ffmpeg 神秘失败，极难定位）。
"""
import pytest

from app.services.workspace import ensure_free_space


async def test_ensure_free_space_raises_when_insufficient():
    with pytest.raises(OSError, match="磁盘空间不足"):
        await ensure_free_space(1 << 60)  # 1 EiB，必然不足
