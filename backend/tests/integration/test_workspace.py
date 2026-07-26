"""workspace 上下文管理器——打真实 dev bucket。"""
import pytest

from tests.integration.conftest_cos import requires_cos

from app.services import object_store
from app.services.workspace import workspace, ensure_free_space

pytestmark = requires_cos


async def test_fetch_then_publish_roundtrip(cos_prefix, tmp_path):
    src = tmp_path / "in.txt"
    src.write_bytes(b"workspace roundtrip")
    key = await object_store.put(f"{cos_prefix}in.txt", src)

    async with workspace() as ws:
        local = await ws.fetch(key)
        assert local.read_bytes() == b"workspace roundtrip"

        out = ws.path("out.txt")
        out.write_bytes(local.read_bytes() + b" processed")
        out_key = await ws.publish(out, f"{cos_prefix}out.txt")

    assert await object_store.exists(out_key)
    dest = tmp_path / "verify.txt"
    await object_store.get(out_key, dest)
    assert dest.read_bytes() == b"workspace roundtrip processed"


async def test_tmpdir_removed_on_exit(cos_prefix, tmp_path):
    src = tmp_path / "x.txt"
    src.write_bytes(b"x")
    key = await object_store.put(f"{cos_prefix}x.txt", src)

    async with workspace() as ws:
        root = ws.root
        await ws.fetch(key)
        assert root.exists()
    assert not root.exists()


async def test_tmpdir_removed_even_on_exception(cos_prefix):
    captured = {}
    with pytest.raises(ValueError):
        async with workspace() as ws:
            captured["root"] = ws.root
            raise ValueError("boom")
    assert not captured["root"].exists()


async def test_fetch_accepts_custom_local_name(cos_prefix, tmp_path):
    src = tmp_path / "y.mp4"
    src.write_bytes(b"video bytes")
    key = await object_store.put(f"{cos_prefix}deep/nested/y.mp4", src)

    async with workspace() as ws:
        local = await ws.fetch(key, name="source.mp4")
        assert local.name == "source.mp4"
        assert local.read_bytes() == b"video bytes"


async def test_ensure_free_space_raises_when_insufficient():
    with pytest.raises(OSError, match="磁盘空间不足"):
        await ensure_free_space(1 << 60)  # 1 EiB，必然不足
