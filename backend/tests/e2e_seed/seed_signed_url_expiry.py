#!/usr/bin/env python3
"""Seed a fresh, isolated project + completed shot 1 with a REAL COS-stored
video, for the signed-URL-expiry e2e
(frontend-vite/e2e/signed-url-expiry.spec.ts).

Runs INSIDE the backend container (real DB, real COS — the container's
DATABASE_URL / COS credentials already point at the live shared volumes /
bucket):

    podman exec video-maker-backend-dev sh -c '
        export COS_SECRET_ID=$(cat /run/secrets/cos_secret_id) &&
        export COS_SECRET_KEY=$(cat /run/secrets/cos_secret_key) &&
        uv run --project /app python \\
            /app/tests/e2e_seed/seed_signed_url_expiry.py "{}"'

Reuses a REAL already-uploaded shot video object from the shared COS bucket
(discovered by listing `projects/*/shots/*/output_*.mp4`) and server-side
COS-copies it into a freshly isolated project's shot key — no model call, no
billing. We can't discover a reusable key via a DB query here: this shared
dev DB's own Shot rows still carry pre-migration local-filesystem-style paths
(`storage/projects/...`), not COS keys, so is_valid_key() rejects every one of
them even though the objects genuinely exist in COS — see COS storage layer
Task 12 backfill scope. Listing the bucket directly sidesteps that.

Prints the new project_id on the last line of stdout.
"""
import asyncio
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

# Running as `python /app/tests/e2e_seed/seed_signed_url_expiry.py` puts the
# script's own dir on sys.path[0], not /app — add the backend root explicitly
# so `app.*` imports resolve regardless of invocation cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db import AsyncSession, init_db
from app.models.project import Project, Shot
from app.services import cos_client, object_store
from app.services.storage import shot_key


async def find_real_source_key() -> str:
    """Locate a real, already-uploaded shot video object in COS to reuse."""
    keys = await object_store.list_prefix("projects/")
    candidates = [
        k for k in keys
        if k.endswith(".mp4") and "/shots/" in k and "/output_" in k
    ]
    if not candidates:
        raise RuntimeError(
            "No real shot output_*.mp4 found under projects/ in COS to reuse"
        )
    candidates.sort()
    return candidates[0]


async def main(args: dict) -> None:
    await init_db()
    await cos_client.warm_credentials()

    src_key = args.get("source_key") or await find_real_source_key()

    project_id = str(uuid.uuid4())
    now = datetime.utcnow()
    dest_key = shot_key(project_id, 1, "output.mp4")
    await object_store.copy(src_key, dest_key)

    async with AsyncSession() as session:
        project = Project(
            id=project_id,
            title="PW Signed URL Expiry",
            theme_text="Playwright e2e: 签名 URL 过期兜底",
            creator_name="e2e-expiry",
            status="shot_review",
            aspect_ratio="9:16",
            scene_overview="测试场景概览：签名 URL 过期兜底 e2e。",
            created_at=now,
            updated_at=now,
        )
        session.add(project)

        shot = Shot(
            project_id=project_id,
            shot_id=1,
            text="签名 URL 过期测试镜头。",
            shot_type="Medium Shot",
            visual_description="Seeded shot for signed-url-expiry e2e.",
            shot_duration=4,
            align_with_previous=False,
            status="completed",
            video_path=dest_key,
            created_at=now,
            updated_at=now,
        )
        session.add(shot)

        await session.commit()

    print(project_id)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: seed_signed_url_expiry.py '<json>'", file=sys.stderr)
        sys.exit(1)
    asyncio.run(main(json.loads(sys.argv[1])))
