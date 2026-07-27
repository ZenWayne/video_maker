#!/usr/bin/env python3
"""
Seed a test project at a given state directly into the SQLite DB.
Usage: python seed.py '<json_args>'
Prints the project_id on the last line of stdout.
"""

import sys
import json
import tempfile
import uuid
import asyncio
from datetime import datetime
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'backend'))

from app.db import AsyncSession, engine, init_db
from app.models.project import Project, Shot, Base
from app.services import cos_client, object_store
from app.services.storage import final_video_key, shot_key
from app.services.storyboard import write_storyboard
from sqlalchemy import select
import os

SAMPLE_SHOTS = [
    {
        "shot_id": 1,
        "text": "主角登场，环顾四周。",
        "shot_type": "Wide Shot",
        "visual_description": "A wide establishing shot of the hero standing in a city square at dusk.",
        "shot_duration": 6,
        "align_with_previous": False,
        "reference_image_hint": None,
    },
    {
        "shot_id": 2,
        "text": "特写镜头，眼神坚定。",
        "shot_type": "Close-up",
        "visual_description": "Close-up of the hero's determined eyes.",
        "shot_duration": 4,
        "align_with_previous": True,
        "reference_image_hint": None,
    },
    {
        "shot_id": 3,
        "text": "转身，踏上征程。这是一段全新的旅程，充满未知与可能。",
        "shot_type": "Medium Shot",
        "visual_description": "Medium shot of the hero turning and walking away.",
        "shot_duration": 8,
        "align_with_previous": False,
        "reference_image_hint": "Upload: a sword prop and a travel map — representing the journey ahead",
    },
]


async def seed(state: str, title: str = "PW Test Project", aspect_ratio: str = "16:9") -> str:
    await init_db()
    # COS is the only store now — nothing below writes to a local
    # storage_root anymore, everything is object_store.put() to a real key.
    # This script runs standalone (no FastAPI lifespan), so it has to warm
    # credentials itself, same as tests/e2e_seed/*.py.
    await cos_client.warm_credentials()

    project_id = str(uuid.uuid4())
    now = datetime.utcnow()

    async with AsyncSession() as session:
        # Create project
        db_status = "shot_review" if state == "shot_review_with_failures" else state
        project = Project(
            id=project_id,
            title=title,
            theme_text="Playwright E2E test project",
            creator_name="pw-test",
            status=db_status,
            aspect_ratio=aspect_ratio,
            created_at=now,
            updated_at=now,
        )

        if state in ("script_review", "shot_generating", "shot_review", "exporting", "exported"):
            project.scene_overview = "测试场景概览：主角踏上征程的故事。"

        session.add(project)

        # Add shots for states that have them
        effective_state = "shot_review" if state == "shot_review_with_failures" else state
        if effective_state in ("script_review", "shot_generating", "shot_review", "exporting", "exported"):
            for shot_data in SAMPLE_SHOTS:
                shot_status = "pending"
                video_path = None
                last_frame_path = None
                error_message = None

                if effective_state in ("shot_review", "exporting", "exported"):
                    shot_status = "completed"
                    # Publish placeholder video/last-frame objects to COS so the
                    # UI has real keys to sign URLs for (first_frame_path is no
                    # longer a Shot column — first frame is derived, not stored
                    # per-shot — so there is nothing to publish for it here).
                    with tempfile.TemporaryDirectory() as td:
                        local_video = Path(td) / "output.mp4"
                        local_last_frame = Path(td) / "last_frame.png"
                        local_video.write_bytes(b'\x00' * 100)
                        local_last_frame.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00' * 50)
                        video_path = await object_store.put(
                            shot_key(project_id, shot_data["shot_id"], "output.mp4"),
                            local_video,
                        )
                        last_frame_path = await object_store.put(
                            shot_key(project_id, shot_data["shot_id"], "last_frame.png"),
                            local_last_frame,
                        )

                # For shot_review_with_failures, make shot 3 failed
                if state == "shot_review_with_failures" and shot_data["shot_id"] == 3:
                    shot_status = "failed"
                    error_message = "400 INVALID_ARGUMENT: Your use case is currently not supported."
                    video_path = None
                    last_frame_path = None

                shot = Shot(
                    project_id=project_id,
                    shot_id=shot_data["shot_id"],
                    text=shot_data["text"],
                    shot_type=shot_data["shot_type"],
                    visual_description=shot_data["visual_description"],
                    shot_duration=shot_data["shot_duration"],
                    align_with_previous=shot_data["align_with_previous"],
                    reference_image_hint=shot_data.get("reference_image_hint"),
                    status=shot_status,
                    video_path=video_path,
                    last_frame_path=last_frame_path,
                    error_message=error_message,
                    word_count_warning=False,
                    motion_prompt=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(shot)

            # Write storyboard.json to COS via the shared helper (same one
            # worker.tasks / app.api.pipeline use) so the key format stays
            # in lockstep with the rest of the app.
            shots_payload = [
                {
                    "shot_id": s["shot_id"],
                    "text": s["text"],
                    "shot_type": s["shot_type"],
                    "visual_description": s["visual_description"],
                    "shot_duration": s["shot_duration"],
                    "align_with_previous": s["align_with_previous"],
                    "reference_image_hint": s.get("reference_image_hint"),
                }
                for s in SAMPLE_SHOTS
            ]
            project.storyboard_path = await write_storyboard(
                project_id, project.scene_overview, shots_payload
            )

        # For exported state, create a final video placeholder
        if state == "exported":
            with tempfile.TemporaryDirectory() as td:
                local_final = Path(td) / "merged.mp4"
                local_final.write_bytes(b'\x00' * 200)
                project.final_video_path = await object_store.put(
                    final_video_key(project_id), local_final
                )

        await session.commit()

    return project_id


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: seed.py '<json>'", file=sys.stderr)
        sys.exit(1)

    args = json.loads(sys.argv[1])
    state = args.get("state", "draft")
    title = args.get("title", f"PW Test [{state}]")
    aspect_ratio = args.get("aspect_ratio", "16:9")

    project_id = asyncio.run(seed(state, title, aspect_ratio))
    # Print ONLY the project_id on the last line so the caller can parse it
    print(project_id)
