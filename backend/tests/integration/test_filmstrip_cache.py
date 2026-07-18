"""filmstrip 端点缓存：确定性文件名复用 + 清理过期 sprite。

背景：旧实现每次打开裁剪弹窗都用 ts_uuid_name() 生成一个新文件名并重新跑
ffmpeg tile —— 每次 dialog-open 都在 shot 目录堆一张 PNG，且重复付出 ffmpeg
成本。修复后文件名按源片路径确定性哈希，同源重复请求应直接复用缓存文件、
不再调用 extract_filmstrip_sprite；且旧 source 遗留的过期 sprite 应被清理，
不随时间无限堆积。
"""
from pathlib import Path

import pytest
from sqlalchemy import select

import app.agents.video_trimmer as vt
from tests.integration.conftest import _make_project
from app.models.project import Shot


async def _seed_shot(sf, tmp_path, pid, video_name="output_1700000000_deadbeef.mp4"):
    sdir = tmp_path / "projects" / pid / "shots" / "shot_1"
    sdir.mkdir(parents=True, exist_ok=True)
    source = sdir / video_name
    source.write_bytes(b"src")
    async with sf() as s:
        s.add(Shot(
            project_id=pid, shot_id=1, text="t", shot_type="Medium Shot",
            visual_description="v", shot_duration=6, status="completed",
            align_with_previous=False, video_path=str(source),
        ))
        await s.commit()
    return str(source), sdir


@pytest.fixture
def sprite_calls(monkeypatch):
    """Mock the (expensive) ffmpeg tile call + the (cheap) ffprobe call it wraps."""
    calls = {"extract": 0}

    def _fake_extract(video_path, out_path, *, count=12, cell_width=96):
        calls["extract"] += 1
        Path(out_path).write_bytes(b"png")
        return count

    monkeypatch.setattr(vt, "extract_filmstrip_sprite", _fake_extract)
    monkeypatch.setattr(
        vt, "get_video_info",
        lambda path: {"fps": 24.0, "total_frames": 240, "duration": 10.0},
    )
    return calls


async def test_repeat_open_reuses_cached_sprite_without_rerunning_ffmpeg(
    client, db_session_factory, tmp_path, sprite_calls
):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _seed_shot(db_session_factory, tmp_path, pid)

    r1 = await client.get(f"/api/projects/{pid}/shots/1/filmstrip")
    r2 = await client.get(f"/api/projects/{pid}/shots/1/filmstrip")

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["url"] == r2.json()["url"], "同源重复请求必须复用同一 sprite 文件"
    assert sprite_calls["extract"] == 1, "第二次打开不应重新跑 ffmpeg tile"


async def test_stale_sprite_from_previous_source_is_cleaned_up(
    client, db_session_factory, tmp_path, sprite_calls
):
    pid = await _make_project(db_session_factory, status="shot_review")
    _source, sdir = await _seed_shot(db_session_factory, tmp_path, pid)

    # Simulate an orphan left by a previous source (e.g. before a re-trim/VC
    # swapped the underlying video) — a filmstrip_*.png that does NOT match
    # the current source's deterministic hash.
    stale = sdir / "filmstrip_0000deadbeef_12.png"
    stale.write_bytes(b"old")

    r = await client.get(f"/api/projects/{pid}/shots/1/filmstrip")

    assert r.status_code == 200
    assert not stale.exists(), "过期 sprite 必须被清理，不能无限堆积"
    # The freshly (re)generated sprite for the current source must still exist.
    assert len(list(sdir.glob("filmstrip_*.png"))) == 1


async def test_different_source_gets_its_own_sprite_and_regenerates(
    client, db_session_factory, tmp_path, sprite_calls
):
    """不同源片（不同哈希）不能复用缓存——必须重新生成。"""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _seed_shot(db_session_factory, tmp_path, pid, video_name="output_a.mp4")
    r1 = await client.get(f"/api/projects/{pid}/shots/1/filmstrip")
    assert sprite_calls["extract"] == 1

    # Point shot at a different physical file (as a re-trim/VC would).
    sdir = tmp_path / "projects" / pid / "shots" / "shot_1"
    new_source = sdir / "output_b.mp4"
    new_source.write_bytes(b"new-src")
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.video_path = str(new_source)
        await s.commit()

    r2 = await client.get(f"/api/projects/{pid}/shots/1/filmstrip")
    assert sprite_calls["extract"] == 2, "换源后必须重新生成，不能误命中旧缓存"
    assert r1.json()["url"] != r2.json()["url"]
