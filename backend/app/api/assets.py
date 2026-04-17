"""Assets API routes for serving static files."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.services.storage import (
    reference_images_dir,
    shot_dir,
    final_video_path,
)

router = APIRouter()


@router.get("/projects/{project_id}/assets/{kind}/{file}")
async def serve_asset(project_id: str, kind: str, file: str):
    """Serve static assets (reference images, shot frames, etc.)."""
    # Security: sanitize file name
    file = Path(file).name

    if kind == "reference_images":
        path = reference_images_dir(project_id) / file
    elif kind.startswith("shots/"):
        # Format: shots/{shot_id}/{filename}
        parts = kind.split("/")
        if len(parts) >= 2:
            shot_id_str = parts[1].replace("shot_", "")
            try:
                shot_id = int(shot_id_str)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid shot ID")
            path = shot_dir(project_id, shot_id) / file
        else:
            raise HTTPException(status_code=400, detail="Invalid shot path")
    elif kind == "final":
        path = final_video_path(project_id).parent / file
    else:
        raise HTTPException(status_code=400, detail="Unknown asset kind")

    if not path.exists():
        raise HTTPException(status_code=404, detail="Asset not found")

    return FileResponse(str(path))


@router.get("/projects/{project_id}/final.mp4")
async def download_final(project_id: str):
    """Download the final merged video."""
    path = final_video_path(project_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Final video not ready")

    return FileResponse(
        str(path),
        media_type="video/mp4",
        filename="merged.mp4",
    )
