"""素材路由。媒体本体存在 COS，本模块只签发重定向，不中转流量。"""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from app.services import object_store
from app.services.storage import (
    final_video_key,
    is_valid_key,
    reference_images_prefix,
    shot_prefix,
)

router = APIRouter()


@router.get("/projects/{project_id}/assets/{kind}/{file}")
async def serve_asset(project_id: str, kind: str, file: str):
    """302 重定向到素材的签名 URL。"""
    file = Path(file).name  # 去掉任何路径成分

    if kind == "reference_images":
        key = f"{reference_images_prefix(project_id)}{file}"
    elif kind.startswith("shots/"):
        parts = kind.split("/")
        if len(parts) < 2:
            raise HTTPException(status_code=400, detail="Invalid shot path")
        try:
            shot_id = int(parts[1].replace("shot_", ""))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid shot ID")
        key = f"{shot_prefix(project_id, shot_id)}{file}"
    elif kind == "final":
        key = f"{final_video_key(project_id).rsplit('/', 1)[0]}/{file}"
    else:
        raise HTTPException(status_code=400, detail="Unknown asset kind")

    if not is_valid_key(key):
        raise HTTPException(status_code=400, detail="Invalid key")
    if not await object_store.exists(key):
        raise HTTPException(status_code=404, detail="Asset not found")

    return RedirectResponse(url=object_store.signed_url(key), status_code=302)


@router.get("/projects/{project_id}/final.mp4")
async def download_final(project_id: str):
    """302 重定向到成片下载 URL。

    附件下载头由 COS 通过 response-content-disposition 直接返回，
    后端完全不参与视频流量。
    """
    key = final_video_key(project_id)
    if not await object_store.exists(key):
        raise HTTPException(status_code=404, detail="Final video not ready")

    return RedirectResponse(
        url=object_store.signed_url(key, filename="merged.mp4"),
        status_code=302,
    )
