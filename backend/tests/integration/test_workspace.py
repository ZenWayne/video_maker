"""workspace 上下文管理器——打真实 dev bucket。"""
import pytest

from tests.integration.conftest_cos import requires_cos

from app.services import object_store
from app.services.workspace import workspace

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


async def test_fetch_accepts_custom_local_name(cos_prefix, tmp_path):
    src = tmp_path / "y.mp4"
    src.write_bytes(b"video bytes")
    key = await object_store.put(f"{cos_prefix}deep/nested/y.mp4", src)

    async with workspace() as ws:
        local = await ws.fetch(key, name="source.mp4")
        assert local.name == "source.mp4"
        assert local.read_bytes() == b"video bytes"


async def test_fetch_same_local_name_different_key_raises(cos_prefix, tmp_path):
    a = tmp_path / "a.wav"
    a.write_bytes(b"shot1 audio")
    b = tmp_path / "b.wav"
    b.write_bytes(b"shot2 audio")
    key_a = await object_store.put(f"{cos_prefix}shot1/audio_original.wav", a)
    key_b = await object_store.put(f"{cos_prefix}shot2/audio_original.wav", b)

    async with workspace() as ws:
        await ws.fetch(key_a)
        with pytest.raises(ValueError):
            await ws.fetch(key_b)


async def test_fetch_same_key_twice_is_idempotent(cos_prefix, tmp_path):
    src = tmp_path / "z.txt"
    src.write_bytes(b"same key twice")
    key = await object_store.put(f"{cos_prefix}z.txt", src)

    async with workspace() as ws:
        first = await ws.fetch(key)
        second = await ws.fetch(key)
        assert first == second
        assert second.read_bytes() == b"same key twice"
