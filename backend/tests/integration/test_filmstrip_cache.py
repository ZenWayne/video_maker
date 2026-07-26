"""filmstrip 端点缓存：确定性 key 复用 + 清理过期 sprite（COS 版）。

背景：旧实现每次打开裁剪弹窗都用 ts_uuid_name() 生成一个新文件名并重新跑
ffmpeg tile —— 每次 dialog-open 都在 shot 目录堆一个文件，且重复付出 ffmpeg
成本。修复后 key 按源片 key + count 确定性哈希，同源同 count 的重复请求应
直接复用缓存对象、不再调用 extract_filmstrip_sprite；且不同 count 遗留的
过期 sprite 应在下次请求时被清理，不随时间无限堆积。

只 mock 昂贵的 ffmpeg tile 调用本身（无关计费，只是省去真实拼图开销）；
真实 COS + 真实 ffprobe（seed_shot_with_source 用 ffmpeg 合成的视频）。
"""
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import pytest

import app.agents.video_trimmer as vt
from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import _make_project, _add_shot, seed_shot_with_source
from app.services import object_store
from app.services.storage import shot_key, shot_prefix

pytestmark = requires_cos


@pytest.fixture
def sprite_calls(monkeypatch):
    """Mock only the expensive ffmpeg tile call; ffprobe (get_video_info) runs for real."""
    calls = {"extract": 0}

    def _fake_extract(video_path, out_path, *, count=12, cell_width=96):
        calls["extract"] += 1
        Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\n fake-sprite")
        return count

    monkeypatch.setattr(vt, "extract_filmstrip_sprite", _fake_extract)
    return calls


async def test_repeat_open_reuses_cached_sprite_without_rerunning_ffmpeg(
    client, db_session_factory, cos_prefix, sprite_calls
):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="completed")
    await seed_shot_with_source(db_session_factory, pid, 1, frames=48)

    r1 = await client.get(f"/api/projects/{pid}/shots/1/filmstrip")
    r2 = await client.get(f"/api/projects/{pid}/shots/1/filmstrip")

    assert r1.status_code == 200 and r2.status_code == 200
    # Signed URLs embed a fresh timestamp/signature per call even for the SAME
    # underlying key — compare the URL *path* (which encodes the COS key),
    # not the full signed URL string.
    assert urlparse(r1.json()["url"]).path == urlparse(r2.json()["url"]).path, \
        "同源重复请求必须复用同一 sprite key"
    assert sprite_calls["extract"] == 1, "第二次打开不应重新跑 ffmpeg tile"


async def test_stale_sprite_for_different_count_is_cleaned_up(
    client, db_session_factory, cos_prefix, sprite_calls
):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="completed")
    await seed_shot_with_source(db_session_factory, pid, 1, frames=48)

    # Simulate an orphan left by a previously requested `count`.
    stale_key = shot_key(pid, 1, "filmstrip_0000deadbeef_12.png")
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "x.png"
        f.write_bytes(b"old")
        await object_store.put(stale_key, f)

    r = await client.get(f"/api/projects/{pid}/shots/1/filmstrip", params={"count": 8})

    assert r.status_code == 200
    assert not await object_store.exists(stale_key), "过期 sprite 必须被清理，不能无限堆积"
    remaining = [
        k for k in await object_store.list_prefix(shot_prefix(pid, 1))
        if k.rsplit("/", 1)[-1].startswith("filmstrip_")
    ]
    assert len(remaining) == 1, "清理后只应剩下当前请求对应的那一个 sprite"


async def test_different_source_gets_its_own_sprite_and_regenerates(
    client, db_session_factory, cos_prefix, sprite_calls
):
    """缓存 key 与源片 key 绑定——换源（即便理论上 Task 8 起 shot.video_path
    不再被物理替换）也不能误命中旧缓存,必须重新生成。"""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="completed")
    await seed_shot_with_source(db_session_factory, pid, 1, frames=48)

    r1 = await client.get(f"/api/projects/{pid}/shots/1/filmstrip")
    assert sprite_calls["extract"] == 1

    # Re-point shot.video_path at a different physical source key.
    await seed_shot_with_source(db_session_factory, pid, 1, frames=90)

    r2 = await client.get(f"/api/projects/{pid}/shots/1/filmstrip")
    assert sprite_calls["extract"] == 2, "换源后必须重新生成，不能误命中旧缓存"
    assert urlparse(r1.json()["url"]).path != urlparse(r2.json()["url"]).path
