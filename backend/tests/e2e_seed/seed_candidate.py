#!/usr/bin/env python3
"""Insert a DONE ImageCandidate backed by a REAL image file (no model call).

Reuses the project's already-uploaded reference image (via the real
POST /reference-images endpoint) by copying it into the shot's candidates
dir — real asset, real DB row, no billed generation.

Runs INSIDE the backend container:

    podman exec video-maker-backend-dev uv run --project /app \\
        python /app/tests/e2e_seed/seed_candidate.py \\
        '{"project_id": "...", "shot_id": 1, "slot": "tail_frame"}'

Prints the candidate_id on the last line.
"""
import asyncio
import json
import sys
from pathlib import Path

# Running as `python /app/tests/e2e_seed/seed_candidate.py` puts the script's
# own dir on sys.path[0], not /app — add the backend root explicitly so
# `app.*` imports resolve regardless of invocation cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import select

from app.db import AsyncSession, init_db
from app.models.project import ImageCandidate, ReferenceImage, Shot
from app.services import cos_client, object_store
from app.services.storage import shot_candidates_prefix, ts_uuid_name


async def main(args: dict) -> str:
    await init_db()
    await cos_client.warm_credentials()
    project_id = args["project_id"]
    shot_seq = int(args["shot_id"])
    slot = args["slot"]

    async with AsyncSession() as session:
        shot = (
            await session.execute(
                select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_seq)
            )
        ).scalar_one()

        ref = (
            await session.execute(
                select(ReferenceImage).where(ReferenceImage.project_id == project_id)
            )
        ).scalars().first()
        if ref is None:
            raise RuntimeError(
                f"No reference image found for project {project_id}; upload one first via the real API"
            )
        src_key = ref.storage_path
        if not await object_store.exists(src_key):
            raise RuntimeError(f"Reference image object missing in COS: {src_key}")

        suffix = Path(src_key).suffix or ".jpg"
        dest_key = f"{shot_candidates_prefix(project_id, shot_seq)}{ts_uuid_name(suffix)}"
        await object_store.copy(src_key, dest_key)

        cand = ImageCandidate(
            project_id=project_id,
            shot_pk=shot.id,
            shot_id=shot_seq,
            slot=slot,
            status="done",
            file_path=dest_key,
            prompt_source="auto",
        )
        session.add(cand)
        await session.commit()
        await session.refresh(cand)
        return cand.id


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: seed_candidate.py '<json>'", file=sys.stderr)
        sys.exit(1)
    print(asyncio.run(main(json.loads(sys.argv[1]))))
