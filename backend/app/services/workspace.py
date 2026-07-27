"""ffmpeg 临时工作区。

COS 是权威存储，本地不保留任何持久文件。需要跑 ffmpeg 时，把输入 fetch
到一次性临时目录，算完 publish 回 COS，退出即删。

用法：
    async with workspace() as ws:
        src = await ws.fetch(shot_video_key(pid, sid))
        out = ws.path("last_frame.png")
        await extract_last_frame(src, out)
        await ws.publish(out, shot_last_frame_key(pid, sid))
"""

import logging
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from app.services import object_store

logger = logging.getLogger(__name__)


async def ensure_free_space(required_bytes: int, at: Optional[Path] = None) -> None:
    """检查可用磁盘空间。不足则抛 OSError。

    导出合并会把整个项目的分镜视频拉到本地，不预检会表现为 ffmpeg 神秘失败。
    """
    target = at or Path(tempfile.gettempdir())
    usage = shutil.disk_usage(target)
    if usage.free < required_bytes:
        raise OSError(
            f"磁盘空间不足：{target} 可用 {usage.free} 字节，需要 {required_bytes} 字节"
        )


class Workspace:
    """一次性临时工作区。不要直接构造，用 workspace() 上下文管理器。"""

    def __init__(self, root: Path):
        self.root = root
        # 本地名 -> 产生它的 key，用于探测同名不同源的静默覆盖
        self._fetched: dict[str, str] = {}

    def path(self, name: str) -> Path:
        """工作区内的新文件路径。不下载任何内容，仅拼路径。

        name 必须是工作区内的相对路径。绝对路径和 .. 都被拒绝——
        Path('/tmp/ws') / '/etc/passwd' 在 pathlib 里会丢弃左操作数
        直接返回 /etc/passwd，不挡住就等于允许写到工作区外面，
        而工作区外的文件不会被退出时的 rmtree 清掉。
        """
        rel = Path(name)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"工作区内文件名必须是不含 .. 的相对路径: {name!r}")
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    async def fetch(self, key: str, name: Optional[str] = None) -> Path:
        """下载 key 到工作区。name 省略时用 key 的最后一段作文件名。

        同一工作区内 fetch 多个 key 时务必显式传 name。项目里有一批固定名
        素材（audio_original.wav、target_last_frame.png 等），不同分镜的
        同类素材最后一段完全相同，用默认名会互相覆盖。下面的守卫把这种静默
        覆盖变成显式报错——出错总好过悄悄 concat 出 N 份同一个视频。
        """
        local_name = name or key.rsplit("/", 1)[-1]
        prev = self._fetched.get(local_name)
        if prev is not None and prev != key:
            raise ValueError(
                f"工作区内本地名 {local_name!r} 已被 key {prev!r} 占用，"
                f"现在又要装入 {key!r}；请为其中之一显式指定 name="
            )
        local = self.path(local_name)
        await object_store.get(key, local)
        self._fetched[local_name] = key
        return local

    async def publish(self, local_path: Path, key: str) -> str:
        """上传工作区文件到 key。返回 key。"""
        return await object_store.put(key, Path(local_path))


@asynccontextmanager
async def workspace():
    """产出一次性 Workspace，退出时无条件删除整个临时目录。"""
    root = Path(tempfile.mkdtemp(prefix="vm_ws_"))
    logger.debug("workspace_created", extra={"root": str(root)})
    try:
        yield Workspace(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        logger.debug("workspace_removed", extra={"root": str(root)})
