import subprocess
from pathlib import Path

import pytest

from app.config import settings
from app.services.storage import shot_output_path
from tests.integration.conftest import HEADERS, _make_project, _add_shot


def _make_tiny_mp4(path: Path) -> None:
    """生成一个 0.5s 的合法小 mp4（带音视频流），供 concat copy 使用。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=0.5",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


async def _add_shot_with_video(db_session_factory, project_id, shot_id):
    """新增一个 completed shot，并生成真实 fixture 视频、写入 video_path。"""
    await _add_shot(db_session_factory, project_id, shot_id, status="completed")
    out = Path(shot_output_path(project_id, shot_id))
    _make_tiny_mp4(out)
    async with db_session_factory() as s:
        from sqlalchemy import select
        from app.models.project import Shot
        row = (
            await s.execute(
                select(Shot).where(
                    Shot.project_id == project_id, Shot.shot_id == shot_id
                )
            )
        ).scalar_one()
        row.video_path = str(out)
        await s.commit()


@pytest.mark.asyncio
async def test_join_preview_success(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    for i in (1, 2, 3):
        await _add_shot_with_video(db_session_factory, pid, i)

    r = await client.post(
        f"/api/projects/{pid}/join-preview",
        json={"shot_ids": [2, 3]},
        headers=HEADERS,
    )

    assert r.status_code == 200, r.text
    url = r.json()["preview_url"]
    assert "/api/media/" in url and "join_preview.mp4" in url
    assert "?t=" in url
    # 实际输出文件已生成
    out = Path(settings.storage_root) / "projects" / pid / "previews" / "join_preview.mp4"
    assert out.is_file() and out.stat().st_size > 0


@pytest.mark.asyncio
async def test_join_preview_requires_two_shots(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot_with_video(db_session_factory, pid, 1)

    r = await client.post(
        f"/api/projects/{pid}/join-preview",
        json={"shot_ids": [1]},
        headers=HEADERS,
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_join_preview_rejects_incomplete_shot(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot_with_video(db_session_factory, pid, 1)
    # shot 2 是 pending、无 video_path
    await _add_shot(db_session_factory, pid, 2, status="pending")

    r = await client.post(
        f"/api/projects/{pid}/join-preview",
        json={"shot_ids": [1, 2]},
        headers=HEADERS,
    )
    assert r.status_code == 400
    assert "2" in r.json()["detail"]


@pytest.mark.asyncio
async def test_join_preview_rejects_missing_video_file(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    # shot 1: normal completed shot with real fixture video
    await _add_shot_with_video(db_session_factory, pid, 1)
    # shot 2: completed shot but video_path points to a non-existent file
    await _add_shot(db_session_factory, pid, 2, status="completed")
    # Set video_path to a non-existent file
    async with db_session_factory() as s:
        from sqlalchemy import select
        from app.models.project import Shot
        row = (
            await s.execute(
                select(Shot).where(
                    Shot.project_id == pid, Shot.shot_id == 2
                )
            )
        ).scalar_one()
        row.video_path = str(Path(shot_output_path(pid, 2)) / "nonexistent.mp4")
        await s.commit()

    r = await client.post(
        f"/api/projects/{pid}/join-preview",
        json={"shot_ids": [1, 2]},
        headers=HEADERS,
    )
    assert r.status_code == 400
    assert "2" in r.json()["detail"]
