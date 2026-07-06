#!/usr/bin/env python3
"""Insert a completed Shot row into an EXISTING project and move it to shot_review.

Runs INSIDE the backend container (real DB, real storage — the container's
DATABASE_URL/storage_root already point at the live named volumes):

    podman exec video-maker-backend-dev uv run --project /app \\
        python /app/tests/e2e_seed/seed_shot_review.py '{"project_id": "..."}'

Prints "ok" on success.
"""
import asyncio
import json
import sys
from pathlib import Path

# Running as `python /app/tests/e2e_seed/seed_shot_review.py` puts the script's
# own dir on sys.path[0], not /app — add the backend root explicitly so
# `app.*` imports resolve regardless of invocation cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import select

from app.db import AsyncSession, init_db
from app.models.project import Project, Shot
from app.services.storage import ensure_shot_dir, shot_dir


async def main(args: dict) -> None:
    await init_db()
    project_id = args["project_id"]
    shot_id = int(args.get("shot_id", 1))

    async with AsyncSession() as session:
        project = (
            await session.execute(select(Project).where(Project.id == project_id))
        ).scalar_one()

        existing = (
            await session.execute(
                select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
            )
        ).scalar_one_or_none()

        if existing is None:
            ensure_shot_dir(project_id, shot_id)
            s_dir = shot_dir(project_id, shot_id)
            video_file = s_dir / "output.mp4"
            last_frame_file = s_dir / "last_frame.png"
            video_file.write_bytes(b"\x00" * 100)
            last_frame_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)

            shot = Shot(
                project_id=project_id,
                shot_id=shot_id,
                text="主角登场，环顾四周。",
                shot_type="Wide Shot",
                visual_description="A wide establishing shot of the hero standing in a city square at dusk.",
                shot_duration=6,
                align_with_previous=False,
                status="completed",
                video_path=str(video_file),
                last_frame_path=str(last_frame_file),
            )
            session.add(shot)

        project.status = "shot_review"
        if not project.scene_overview:
            project.scene_overview = "测试场景概览：Playwright e2e 候选采纳链路。"
        session.add(project)
        await session.commit()

    print("ok")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: seed_shot_review.py '<json>'", file=sys.stderr)
        sys.exit(1)
    asyncio.run(main(json.loads(sys.argv[1])))
