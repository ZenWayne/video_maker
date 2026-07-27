#!/usr/bin/env python3
"""Seed a fresh, isolated project + completed shot 1 with a REAL video for the
TrimDialog dual-track e2e (frontend-vite/e2e/trim-dual-track.spec.ts).

Runs INSIDE the backend container (real DB, real COS bucket):

    podman exec video-maker-backend-dev uv run --project /app \\
        python /app/tests/e2e_seed/seed_trim_dual_track.py '{}'

Optionally pass {"source_video_key": "projects/<pid>/shots/shot_<n>/output_....mp4"}
(a real COS key, not a local path — there is no local storage anymore) to pin
a specific source clip.

When omitted, the script discovers a real already-generated video via the DB
(``Shot.video_path IS NOT NULL``, most-recently-updated first) rather than by
scanning the object store. This is a deliberate design choice, not a literal
port of the pre-COS version (which used to glob the local storage_root — see
git history): that version's local directory scan worked by coincidence,
because a local filesystem doubles as a free, always-current index of "which
files exist". COS has no equivalent cheap operation — the closest analog,
``object_store.list_prefix``, would mean listing the WHOLE bucket (unbounded,
paginated at 1000 keys/page, plus a HEAD call per candidate to check size)
every single e2e run, since this script deliberately doesn't know which
project/shot to look under ahead of time. The DB already tracks exactly which
shots have a real, completed video — querying it (an O(1) indexed lookup) is
the object-store-native replacement for what used to be a directory glob, not
a scan.

No model call, no billing — the discovered video is COS-server-side-copied
(object_store.copy, zero local byte traffic) into the new shot's key. The only
local I/O is a one-time fetch of the freshly-copied video to read real
fps/frame-count via ffprobe (get_video_info, the same helper the trim endpoint
itself uses), so the seeded source_fps/source_frames are accurate.

Prints the new project_id on the last line of stdout.
"""
import asyncio
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

# Running as `python /app/tests/e2e_seed/seed_trim_dual_track.py` puts the
# script's own dir on sys.path[0], not /app — add the backend root explicitly
# so `app.*` imports resolve regardless of invocation cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import desc, select

from app.db import AsyncSession, init_db
from app.models.project import Project, Shot
from app.services import cos_client, object_store
from app.services.storage import shot_key, ts_uuid_name
from app.services.workspace import workspace

# Skip tiny placeholder stubs written by other seed scripts (e.g.
# seed_shot_review.py writes 100-byte stand-ins) — the waveform/filmstrip
# tracks need a genuine multi-second clip with real audio to render.
MIN_REAL_VIDEO_BYTES = 50_000

# How many most-recently-updated shots (that have a video_path at all) to
# probe before giving up. The DB query itself is a single indexed SELECT;
# this only bounds how many object_store existence/size checks we're willing
# to make against DB rows that might point at stale/missing COS objects.
CANDIDATE_BATCH = 50


async def _find_real_source(session) -> tuple[str, str | None]:
    """Return (video_key, last_frame_key_or_None) for a real, already-generated
    shot video — discovered via the DB (the authoritative record of which
    shots have a real video), not by scanning the object store.

    DB rows can point at objects that no longer exist in COS (e.g. a stale
    row from a project whose delete's COS cleanup partially failed — see
    object_store.delete_prefix's docstring on "sooner leave an orphan object
    than lie about success" — or just old local dev data). Each candidate is
    therefore verified with a real object_store.exists() + size() call before
    being trusted; the first one that checks out wins.
    """
    result = await session.execute(
        select(Shot)
        .where(Shot.video_path.isnot(None))
        .order_by(desc(Shot.updated_at))
        .limit(CANDIDATE_BATCH)
    )
    candidates = result.scalars().all()

    checked = 0
    for shot in candidates:
        checked += 1
        key = shot.video_path
        if not await object_store.exists(key):
            continue
        try:
            size = await object_store.size(key)
        except FileNotFoundError:
            continue
        if size < MIN_REAL_VIDEO_BYTES:
            continue
        return key, shot.last_frame_path

    raise RuntimeError(
        f"没有可用的已生成视频可复用（检查了最近 {checked} 个带 video_path 的分镜，"
        "全部缺失于 COS / 是占位桩 / 小于 50KB）。请先在任意项目里真实生成至少一个"
        "分镜（例如用 video-maker-dialogue MCP 的 start_generation，或直接在前端"
        "走一遍生成），再重新运行本脚本；也可以显式传 "
        '{"source_video_key": "projects/<pid>/shots/shot_<n>/output_....mp4"} '
        "指定一个你已知存在的真实视频 key，跳过自动发现。"
    )


async def main(args: dict) -> None:
    await init_db()
    await cos_client.warm_credentials()

    async with AsyncSession() as session:
        if args.get("source_video_key"):
            source_key = args["source_video_key"]
            if not await object_store.exists(source_key):
                raise FileNotFoundError(f"source_video_key not found in COS: {source_key}")
            source_last_frame_key = None
        else:
            source_key, source_last_frame_key = await _find_real_source(session)

    project_id = str(uuid.uuid4())
    now = datetime.utcnow()

    # COS server-side copy — zero local byte traffic for the video itself.
    dest_video_key = shot_key(project_id, 1, f"output_{ts_uuid_name('.mp4')}")
    await object_store.copy(source_key, dest_video_key)

    # Best-effort: carry over the source shot's last_frame too (purely
    # cosmetic — the shot card thumbnail — not load-bearing for the trim
    # dialog itself, which re-derives everything from the video).
    dest_frame_key = None
    if source_last_frame_key and await object_store.exists(source_last_frame_key):
        suffix = Path(source_last_frame_key).suffix or ".png"
        dest_frame_key = shot_key(project_id, 1, f"last_frame_{ts_uuid_name(suffix)}")
        await object_store.copy(source_last_frame_key, dest_frame_key)

    # One-time local fetch purely to read real fps/frame-count via ffprobe —
    # the same get_video_info helper the trim endpoint itself uses.
    from app.agents.video_trimmer import get_video_info

    async with workspace() as ws:
        local_video = await ws.fetch(dest_video_key, name="source.mp4")
        info = get_video_info(str(local_video))
    fps = info["fps"]
    total_frames = info["total_frames"]

    async with AsyncSession() as session:
        project = Project(
            id=project_id,
            title="PW TrimDialog DualTrack",
            theme_text="Playwright e2e: 双轨裁剪线拖拽",
            creator_name="e2e-dual",
            status="shot_review",
            aspect_ratio="9:16",
            scene_overview="测试场景概览：双轨裁剪线拖拽 e2e。",
            created_at=now,
            updated_at=now,
        )
        session.add(project)

        shot = Shot(
            project_id=project_id,
            shot_id=1,
            text="双轨裁剪测试镜头。",
            shot_type="Medium Shot",
            visual_description="Seeded shot for trim-dual-track e2e.",
            shot_duration=6,
            align_with_previous=False,
            status="completed",
            video_path=dest_video_key,
            last_frame_path=dest_frame_key,
            word_count_warning=False,
            source_fps=fps,
            source_frames=total_frames,
            trim_frames=None,
            created_at=now,
            updated_at=now,
        )
        session.add(shot)

        await session.commit()

    print(project_id)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: seed_trim_dual_track.py '<json>'", file=sys.stderr)
        sys.exit(1)
    asyncio.run(main(json.loads(sys.argv[1])))
