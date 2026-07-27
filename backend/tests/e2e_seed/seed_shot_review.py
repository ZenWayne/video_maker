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
import tempfile
from pathlib import Path

# Running as `python /app/tests/e2e_seed/seed_shot_review.py` puts the script's
# own dir on sys.path[0], not /app — add the backend root explicitly so
# `app.*` imports resolve regardless of invocation cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import select

from app.db import AsyncSession, init_db
from app.models.project import Project, Shot
from app.services import cos_client, object_store
from app.services.storage import shot_key


async def main(args: dict) -> None:
    await init_db()
    await cos_client.warm_credentials()
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
            # COS is the only store now — publish real (if tiny) objects instead
            # of writing local files (there is no local storage_root anymore).
            with tempfile.TemporaryDirectory() as td:
                local_video = Path(td) / "output.mp4"
                local_frame = Path(td) / "last_frame.png"
                local_video.write_bytes(b"\x00" * 100)
                local_frame.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
                video_key = await object_store.put(
                    shot_key(project_id, shot_id, "output.mp4"), local_video
                )
                last_frame_key = await object_store.put(
                    shot_key(project_id, shot_id, "last_frame.png"), local_frame
                )

            shot = Shot(
                project_id=project_id,
                shot_id=shot_id,
                text="主角登场，环顾四周。",
                shot_type="Wide Shot",
                visual_description="A wide establishing shot of the hero standing in a city square at dusk.",
                shot_duration=6,
                align_with_previous=False,
                status="completed",
                video_path=video_key,
                last_frame_path=last_frame_key,
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
