#!/usr/bin/env python3
"""Seed a fresh, isolated project + completed shot 1 with a REAL video for the
TrimDialog dual-track e2e (frontend-vite/e2e/trim-dual-track.spec.ts).

Runs INSIDE the backend container (real DB, real storage volumes):

    podman exec video-maker-backend-dev uv run --project /app \\
        python /app/tests/e2e_seed/seed_trim_dual_track.py '{}'

Optionally pass `{"source_video": "/app/storage/projects/.../output_....mp4"}`
(an absolute path INSIDE the container) to pin a specific source clip; when
omitted, the script discovers any already-generated `output_*.mp4` under the
shared storage volume itself (no hardcoded project id / path — mirrors the
dynamic-discovery approach in tests/e2e/nondestructive-real.spec.ts) and copies
that — no model call, no billing.

Real fps/frame-count are read via ffprobe (get_video_info), the same helper the
trim endpoint itself uses, so the seeded source_fps/source_frames are accurate.

Prints the new project_id on the last line of stdout.
"""
import asyncio
import json
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

# Running as `python /app/tests/e2e_seed/seed_trim_dual_track.py` puts the
# script's own dir on sys.path[0], not /app — add the backend root explicitly
# so `app.*` imports resolve regardless of invocation cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings
from app.db import AsyncSession, init_db
from app.models.project import Project, Shot
from app.services.storage import ensure_project_dirs, ensure_shot_dir, shot_dir, ts_uuid_name


def find_real_source_video() -> Path:
    """Locate any already-generated `output_*.mp4` in the shared storage volume.

    Skips tiny placeholder files written by other seed scripts (e.g.
    seed_shot_review.py writes 100-byte stubs) so we copy a genuine multi-second
    clip with real audio — required for the waveform/filmstrip tracks to render.
    """
    root = Path(settings.storage_root) / "projects"
    candidates = [
        p for p in root.glob("*/shots/shot_*/output_*.mp4")
        if p.is_file() and p.stat().st_size > 50_000
    ]
    if not candidates:
        raise FileNotFoundError(
            "No real output_*.mp4 (>50KB) found under storage/projects to seed from"
        )
    # Prefer the largest — most likely a genuine generated clip, not a stub.
    candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    return candidates[0]


async def main(args: dict) -> None:
    await init_db()

    source_video = Path(args["source_video"]) if args.get("source_video") else find_real_source_video()
    if not source_video.exists():
        raise FileNotFoundError(f"source_video not found: {source_video}")

    from app.agents.video_trimmer import get_video_info

    project_id = str(uuid.uuid4())
    now = datetime.utcnow()

    ensure_project_dirs(project_id)
    ensure_shot_dir(project_id, 1)
    s_dir = shot_dir(project_id, 1)

    # Copy the real, already-generated video into the new isolated shot dir,
    # keeping the `output_*.mp4` naming so shot_source_path()'s glob finds it.
    dest_video = s_dir / f"output_{ts_uuid_name('.mp4')}"
    shutil.copyfile(source_video, dest_video)

    info = get_video_info(str(dest_video))
    fps = info["fps"]
    total_frames = info["total_frames"]

    # Best-effort: copy a matching last_frame if one sits next to the source
    # video (purely cosmetic — the shot card thumbnail — not load-bearing for
    # the trim dialog itself, which re-derives everything from the video).
    last_frame_path = None
    sibling_frames = sorted(source_video.parent.glob("last_frame_*.png"))
    if sibling_frames:
        dest_frame = s_dir / f"last_frame_{ts_uuid_name('.png')}"
        shutil.copyfile(sibling_frames[-1], dest_frame)
        last_frame_path = str(dest_frame)

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
            video_path=str(dest_video),
            last_frame_path=last_frame_path,
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
