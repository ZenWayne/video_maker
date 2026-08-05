"""项目素材的 COS key 工具。

COS 是权威存储。本模块只负责拼 key 与生成浏览器可访问的签名 URL；
任何本地文件都只存在于 workspace 的一次性临时目录中。

key 布局与迁移前的 storage_root 相对路径逐字符一致，因此存量迁移是
「本地相对路径 = key」的直接映射。
"""

import logging
import time
import uuid
from typing import Optional

from app.services import object_store

logger = logging.getLogger(__name__)


def ts_uuid_name(ext: str = ".png") -> str:
    """带时间戳的唯一文件名：``<unix_seconds>_<8hex><ext>``。

    保证 key 唯一，同时让浏览器/CDN 永远拿不到过期缓存。
    """
    return f"{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"


# ── 前缀 ──────────────────────────────────────────────────────────────────────

def project_prefix(project_id: str) -> str:
    return f"projects/{project_id}/"


def reference_images_prefix(project_id: str) -> str:
    return f"{project_prefix(project_id)}reference_images/"


def shots_prefix(project_id: str) -> str:
    return f"{project_prefix(project_id)}shots/"


def shot_prefix(project_id: str, shot_id: int) -> str:
    return f"{shots_prefix(project_id)}shot_{shot_id}/"


def shot_candidates_prefix(project_id: str, shot_id: int) -> str:
    return f"{shot_prefix(project_id, shot_id)}candidates/"


def shot_custom_frames_prefix(project_id: str, shot_id: int) -> str:
    return f"{shot_prefix(project_id, shot_id)}custom_frames/"


# ── 分镜级 key ────────────────────────────────────────────────────────────────

def shot_key(project_id: str, shot_id: int, filename: str) -> str:
    """分镜目录下任意文件的 key。用于 ts_uuid_name 生成的唯一名文件。"""
    return f"{shot_prefix(project_id, shot_id)}{filename}"


def shot_audio_original_key(project_id: str, shot_id: int) -> str:
    return shot_key(project_id, shot_id, "audio_original.wav")


def shot_audio_vc_key(project_id: str, shot_id: int) -> str:
    return shot_key(project_id, shot_id, "audio_vc.wav")


def shot_target_last_frame_key(project_id: str, shot_id: int) -> str:
    return shot_key(project_id, shot_id, "target_last_frame.png")


def motion_prompt_key(project_id: str, shot_id: int) -> str:
    return shot_key(project_id, shot_id, "motion_prompt.txt")


# ── 内容分析（爆款归因）key ──────────────────────────────────────────────────

def analysis_prefix(analysis_id: str) -> str:
    return f"analyses/{analysis_id}/"


def sample_prefix(analysis_id: str, sample_id) -> str:
    return f"{analysis_prefix(analysis_id)}sample_{sample_id}/"


def sample_video_key(analysis_id: str, sample_id, filename: str) -> str:
    return f"{sample_prefix(analysis_id, sample_id)}{filename}"


# ── 项目级 key ────────────────────────────────────────────────────────────────

def storyboard_key(project_id: str) -> str:
    return f"{project_prefix(project_id)}storyboard.json"


def archived_storyboard_key(project_id: str, timestamp: str) -> str:
    return f"{project_prefix(project_id)}storyboard_{timestamp}.json"


def final_video_key(project_id: str) -> str:
    return f"{project_prefix(project_id)}final/merged.mp4"


def join_preview_key(project_id: str) -> str:
    return f"{project_prefix(project_id)}previews/join_preview.mp4"


def reference_image_key(project_id: str, image_id: str, filename: str) -> str:
    return f"{reference_images_prefix(project_id)}{image_id}_{filename}"


def reference_voice_prompt_key(project_id: str) -> str:
    """VC 音色克隆的基准 prompt wav（上传的项目级基准音色，与迁移前的
    reference_voice/prompt.wav 相对路径逐字符一致）。"""
    return f"{project_prefix(project_id)}reference_voice/prompt.wav"


# ── 校验与 URL ────────────────────────────────────────────────────────────────

def is_valid_key(key: str) -> bool:
    """key 安全校验：必须在 projects/ 下，且不含路径穿越。

    取代旧的 validate_safe_path()——后者用 str.startswith 判断路径包含关系，
    会把 /storage-evil 误判为位于 /storage 内。key 化后该问题不复存在。
    """
    if not key or key.startswith("/"):
        return False
    if ".." in key.split("/"):
        return False
    return key.startswith("projects/")


def to_media_url(key: Optional[str]) -> Optional[str]:
    """把 COS key 转成浏览器可直接访问的预签名 URL。

    保持**同步**——projects.py 的 _shot_to_dict / _candidate_to_dict 是同步
    序列化器且在列表推导中调用本函数，改 async 会连锁污染全部上游。
    签名是纯本地 HMAC 计算，不发网络请求，同步调用不阻塞事件循环。
    """
    if not key:
        return None
    if not is_valid_key(key):
        # 到了 Task 12,所有写路径都已产出 key;此时还拿到非 key 的值就是真
        # bug。但不能抛异常:本函数在约 50 处同步序列化器里被调用,抛出会把
        # 一行陈旧数据放大成整个项目详情接口 500——在 Spec B 的回填窗口期
        # 尤其糟。优雅降级 + 可观测才是对的取舍。
        logger.warning("to_media_url_invalid_key", extra={"value": key[:200]})
        return None
    return object_store.signed_url(key)
