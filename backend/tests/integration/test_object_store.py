"""object_store 原语——打真实 dev bucket，唯一前缀隔离。"""
import asyncio
import time

import httpx
import pytest

from tests.integration.conftest_cos import requires_cos

from app.services import object_store

pytestmark = requires_cos


async def test_put_then_get_roundtrip(cos_prefix, tmp_path):
    src = tmp_path / "a.txt"
    src.write_bytes(b"hello cos")
    key = await object_store.put(f"{cos_prefix}a.txt", src)

    dest = tmp_path / "back.txt"
    await object_store.get(key, dest)
    assert dest.read_bytes() == b"hello cos"


async def test_exists_and_size(cos_prefix, tmp_path):
    src = tmp_path / "b.bin"
    src.write_bytes(b"x" * 1234)
    key = await object_store.put(f"{cos_prefix}b.bin", src)

    assert await object_store.exists(key) is True
    assert await object_store.size(key) == 1234
    assert await object_store.exists(f"{cos_prefix}nope.bin") is False


async def test_size_missing_raises(cos_prefix):
    with pytest.raises(FileNotFoundError):
        await object_store.size(f"{cos_prefix}missing.bin")


async def test_copy_is_server_side(cos_prefix, tmp_path):
    src = tmp_path / "c.txt"
    src.write_bytes(b"copy me")
    key = await object_store.put(f"{cos_prefix}c.txt", src)

    dst = await object_store.copy(key, f"{cos_prefix}c_backup.txt")
    assert await object_store.exists(dst)

    back = tmp_path / "c2.txt"
    await object_store.get(dst, back)
    assert back.read_bytes() == b"copy me"


async def test_delete_is_idempotent(cos_prefix, tmp_path):
    src = tmp_path / "d.txt"
    src.write_bytes(b"bye")
    key = await object_store.put(f"{cos_prefix}d.txt", src)

    await object_store.delete(key)
    assert await object_store.exists(key) is False
    await object_store.delete(key)  # 第二次不应抛异常


async def test_list_and_delete_prefix(cos_prefix, tmp_path):
    for i in range(3):
        f = tmp_path / f"e{i}.txt"
        f.write_bytes(b"z")
        await object_store.put(f"{cos_prefix}sub/e{i}.txt", f)

    keys = await object_store.list_prefix(f"{cos_prefix}sub/")
    assert len(keys) == 3
    assert all(k.startswith(f"{cos_prefix}sub/") for k in keys)

    n = await object_store.delete_prefix(f"{cos_prefix}sub/")
    assert n == 3
    assert await object_store.list_prefix(f"{cos_prefix}sub/") == []


async def test_signed_url_actually_fetches_content(cos_prefix, tmp_path):
    """断言 URL 真能取到内容，而非断言字符串形态——签名算错时形态照样对。"""
    src = tmp_path / "f.txt"
    src.write_bytes(b"signed content")
    key = await object_store.put(f"{cos_prefix}f.txt", src)

    url = object_store.signed_url(key)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url)
    assert r.status_code == 200
    assert r.content == b"signed content"


async def test_transfers_do_not_block_event_loop(cos_prefix, tmp_path):
    """SDK 是同步的，传输必须在线程池里跑。

    若某个方法漏了 asyncio.to_thread，上传期间事件循环会被独占，
    并发的心跳协程推进次数会显著下降。这是本方案最隐蔽的一类缺陷，
    且只在文件够大时才复现，故用 8MB 文件放大信号。
    """
    big = tmp_path / "big.bin"
    big.write_bytes(b"0" * (8 * 1024 * 1024))

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    hb = asyncio.create_task(heartbeat())
    try:
        await object_store.put(f"{cos_prefix}big.bin", big)
    finally:
        hb.cancel()

    # 事件循环若被阻塞，ticks 会接近 0
    assert ticks > 5, f"事件循环疑似被阻塞，心跳仅推进 {ticks} 次"
