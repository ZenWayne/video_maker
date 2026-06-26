"""Storage path utilities for project files."""

import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

from app.config import settings


def ts_uuid_name(ext: str = ".png") -> str:
    """Timestamped unique filename: ``<unix_seconds>_<8hex>.<ext>``.

    Each call is unique, so user-uploaded/extracted keyframes get a fresh URL
    and the browser never serves a cached stale frame.
    """
    return f"{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"


def project_dir(project_id: str) -> Path:
    """Get the project storage directory."""
    return Path(settings.storage_root) / "projects" / project_id


def reference_images_dir(project_id: str) -> Path:
    """Get the reference images directory for a project."""
    return project_dir(project_id) / "reference_images"


def shots_dir(project_id: str) -> Path:
    """Get the shots directory for a project."""
    return project_dir(project_id) / "shots"


def shot_dir(project_id: str, shot_id: int) -> Path:
    """Get the directory for a specific shot."""
    return shots_dir(project_id) / f"shot_{shot_id}"


def shot_custom_frames_dir(project_id: str, shot_id: int) -> Path:
    """Get the custom reference frames directory for a shot."""
    return shot_dir(project_id, shot_id) / "custom_frames"


def shot_audio_original_path(project_id: str, shot_id: int) -> Path:
    """Get the original audio WAV path for a shot (extracted from unmodified video)."""
    return shot_dir(project_id, shot_id) / "audio_original.wav"


def shot_audio_vc_path(project_id: str, shot_id: int) -> Path:
    """Get the voice-converted audio WAV path for a shot."""
    return shot_dir(project_id, shot_id) / "audio_vc.wav"


def shot_pre_vc_video_path(project_id: str, shot_id: int) -> Path:
    """Get the pre-VC backup video path for a shot."""
    return shot_dir(project_id, shot_id) / "output_pre_vc.mp4"


def shot_pre_cc_last_frame_path(project_id: str, shot_id: int) -> Path:
    """Get the pre-character-calibration backup of last_frame.png for a shot."""
    return shot_dir(project_id, shot_id) / "last_frame_pre_cc.png"


def shot_target_last_frame_path(project_id: str, shot_id: int) -> Path:
    """Get the AI-generated target tail frame path for a shot."""
    return shot_dir(project_id, shot_id) / "target_last_frame.png"


def get_original_video_for_audio(project_id: str, shot_id: int) -> Path:
    """Get the un-VC'd video to extract audio from.

    Priority: output_pre_vc.mp4 (VC backup, post-trim) > newest
    output_<ts>_<uuid>.mp4 (current, uniquely-named).
    NOT output_original.mp4 — that is the pre-trim backup and has different
    duration, which would cause lip sync mismatch after trimming.
    """
    s_dir = shot_dir(project_id, shot_id)
    pre_vc = s_dir / "output_pre_vc.mp4"
    if pre_vc.exists():
        return pre_vc
    uniques = [
        p for p in s_dir.glob("output_*.mp4")
        if p.name not in ("output_original.mp4", "output_pre_vc.mp4")
    ]
    if uniques:
        return max(uniques, key=lambda p: p.stat().st_mtime)
    raise FileNotFoundError(f"No video found in {s_dir}")


def final_dir(project_id: str) -> Path:
    """Get the final output directory for a project."""
    return project_dir(project_id) / "final"


def storyboard_path(project_id: str) -> Path:
    """Get the storyboard.json path for a project."""
    return project_dir(project_id) / "storyboard.json"


def archived_storyboard_path(project_id: str, timestamp: str) -> Path:
    """Get the archived storyboard path with timestamp."""
    return project_dir(project_id) / f"storyboard_{timestamp}.json"


def motion_prompt_path(project_id: str, shot_id: int) -> Path:
    """Get the motion_prompt.txt path for a shot."""
    return shot_dir(project_id, shot_id) / "motion_prompt.txt"


def shot_output_path(project_id: str, shot_id: int) -> Path:
    """Get the output.mp4 path for a shot."""
    return shot_dir(project_id, shot_id) / "output.mp4"


def shot_last_frame_path(project_id: str, shot_id: int) -> Path:
    """Get the last_frame.png path for a shot."""
    return shot_dir(project_id, shot_id) / "last_frame.png"


def final_video_path(project_id: str) -> Path:
    """Get the merged.mp4 path for a project."""
    return final_dir(project_id) / "merged.mp4"


def join_preview_path(project_id: str) -> Path:
    """临时连贯性预览视频的固定输出路径（每次覆盖）。"""
    previews_dir = project_dir(project_id) / "previews"
    previews_dir.mkdir(parents=True, exist_ok=True)
    return previews_dir / "join_preview.mp4"


def reference_image_path(project_id: str, image_id: str, filename: str) -> Path:
    """Get the storage path for a reference image."""
    return reference_images_dir(project_id) / f"{image_id}_{filename}"


def ensure_project_dirs(project_id: str) -> None:
    """Create all necessary directories for a project."""
    project_dir(project_id).mkdir(parents=True, exist_ok=True)
    reference_images_dir(project_id).mkdir(exist_ok=True)
    shots_dir(project_id).mkdir(exist_ok=True)
    final_dir(project_id).mkdir(exist_ok=True)


def ensure_shot_dir(project_id: str, shot_id: int) -> None:
    """Create directory for a specific shot."""
    shot_dir(project_id, shot_id).mkdir(parents=True, exist_ok=True)


def delete_project_storage(project_id: str) -> None:
    """Delete all storage for a project."""
    proj_dir = project_dir(project_id)
    if proj_dir.exists():
        shutil.rmtree(proj_dir)


def get_storage_relative_path(absolute_path: str) -> Optional[str]:
    """Convert absolute path to storage-relative path."""
    storage_root = Path(settings.storage_root)
    try:
        return str(Path(absolute_path).relative_to(storage_root))
    except ValueError:
        return None


def to_media_url(absolute_path: Optional[str]) -> Optional[str]:
    """Convert an absolute storage path to a /api/media/... URL for the browser."""
    if not absolute_path:
        return None
    storage_root = Path(settings.storage_root).resolve()
    try:
        rel = Path(absolute_path).resolve().relative_to(storage_root)
        return f"/api/media/{rel}"
    except ValueError:
        return None


def validate_safe_path(path: str) -> bool:
    """
    Validate that a path is safe (no path traversal).
    Returns True if safe, False otherwise.
    """
    try:
        # Resolve to absolute path
        resolved = Path(path).resolve()
        storage_root = Path(settings.storage_root).resolve()

        # Check if resolved path is within storage root
        return str(resolved).startswith(str(storage_root))
    except (ValueError, RuntimeError):
        return False
