#!/usr/bin/env python3
"""
Seed a test project at a given state directly into the SQLite DB.
Usage: python seed.py '<json_args>'
Prints the project_id on the last line of stdout.
"""

import sys
import json
import uuid
import asyncio
from datetime import datetime
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'backend'))

from app.db import AsyncSession, engine, init_db
from app.models.project import Project, Shot, Base
from app.services.storage import (
    project_dir, reference_images_dir, shot_dir,
    final_dir, storyboard_path, ensure_project_dirs, ensure_shot_dir
)
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

        # Ensure storage dirs
        ensure_project_dirs(project_id)

        # Add shots for states that have them
        effective_state = "shot_review" if state == "shot_review_with_failures" else state
        if effective_state in ("script_review", "shot_generating", "shot_review", "exporting", "exported"):
            for shot_data in SAMPLE_SHOTS:
                shot_status = "pending"
                video_path = None
                first_frame_path = None
                last_frame_path = None
                error_message = None

                if effective_state in ("shot_review", "exporting", "exported"):
                    shot_status = "completed"
                    # Create placeholder video files so the UI can reference them
                    ensure_shot_dir(project_id, shot_data["shot_id"])
                    shot_storage_dir = shot_dir(project_id, shot_data["shot_id"])
                    video_file = shot_storage_dir / "output.mp4"
                    first_frame_file = shot_storage_dir / "first_frame.png"
                    last_frame_file = shot_storage_dir / "last_frame.png"
                    # Write minimal placeholder files
                    video_file.write_bytes(b'\x00' * 100)
                    first_frame_file.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00' * 50)
                    last_frame_file.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00' * 50)
                    video_path = str(video_file)
                    first_frame_path = str(first_frame_file)
                    last_frame_path = str(last_frame_file)

                # For shot_review_with_failures, make shot 3 failed
                if state == "shot_review_with_failures" and shot_data["shot_id"] == 3:
                    shot_status = "failed"
                    error_message = "400 INVALID_ARGUMENT: Your use case is currently not supported."
                    video_path = None
                    first_frame_path = None
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
                    first_frame_path=first_frame_path,
                    last_frame_path=last_frame_path,
                    error_message=error_message,
                    word_count_warning=False,
                    motion_prompt=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(shot)

            # Write storyboard.json
            storyboard_data = {
                "scene_overview": project.scene_overview,
                "shots": [
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
                ],
            }
            sb_path = storyboard_path(project_id)
            sb_path.write_text(json.dumps(storyboard_data, ensure_ascii=False))
            project.storyboard_path = str(sb_path)

        # For exported state, create a final video placeholder
        if state == "exported":
            final_storage_dir = final_dir(project_id)
            final_storage_dir.mkdir(parents=True, exist_ok=True)
            final_video = final_storage_dir / "merged.mp4"
            final_video.write_bytes(b'\x00' * 200)
            project.final_video_path = str(final_video)

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
