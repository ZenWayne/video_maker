"""Pydantic schemas for request/response validation."""

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator

from app.models.project import ProjectStatus, ShotStatus, ReferenceImageKind


# ============== Reference Image Schemas ==============

class ReferenceImageCreate(BaseModel):
    kind: ReferenceImageKind


class ReferenceImageResponse(BaseModel):
    id: str
    kind: str
    filename: str
    storage_path: str
    order_index: int
    created_at: datetime

    class Config:
        from_attributes = True


# ============== Shot Schemas ==============

class ShotItem(BaseModel):
    """Shot item in storyboard."""
    shot_id: int
    text: str
    shot_type: str = Field(..., pattern="^(Close-up|Medium Shot|Wide Shot)$")
    visual_description: str
    shot_duration: int = Field(..., ge=4, le=8)
    align_with_previous: bool = True
    reference_image_hint: Optional[str] = None


class ShotResponse(BaseModel):
    id: int
    shot_id: int
    text: str
    shot_type: str
    visual_description: str
    shot_duration: int
    status: str
    align_with_previous: bool
    use_prev_last_frame: bool = False
    motion_prompt: Optional[str] = None
    first_frame_path: Optional[str] = None
    video_path: Optional[str] = None
    last_frame_path: Optional[str] = None
    word_count_warning: bool
    error_message: Optional[str] = None
    custom_first_frame_path: Optional[str] = None
    custom_reference_paths: Optional[List[str]] = None
    reference_image_hint: Optional[str] = None
    vc_status: Optional[str] = None
    vc_error_message: Optional[str] = None
    cc_status: Optional[str] = None
    cc_error_message: Optional[str] = None
    target_last_frame_path: Optional[str] = None
    tf_status: Optional[str] = None
    tf_error_message: Optional[str] = None
    tf_confirmed: bool = False
    auto_trim: bool = True
    # Non-destructive editing (EDL) playback descriptor
    trim_frames: Optional[int] = None
    source_fps: Optional[float] = None
    source_frames: Optional[int] = None
    trim_end_sec: Optional[float] = None
    vc_audio_url: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ShotUpdate(BaseModel):
    motion_prompt: Optional[str] = None
    text: Optional[str] = None
    visual_description: Optional[str] = None
    align_with_previous: Optional[bool] = None
    use_prev_last_frame: Optional[bool] = None
    shot_duration: Optional[int] = Field(default=None, ge=4, le=8)
    auto_trim: Optional[bool] = None


class ShotTrimRequest(BaseModel):
    end_frame: int = Field(..., ge=1)


class ShotAiEditRequest(BaseModel):
    instruction: str


class ShotEdit(BaseModel):
    """For editing shot in script review."""
    text: Optional[str] = None
    shot_type: Optional[str] = None
    visual_description: Optional[str] = None
    shot_duration: Optional[int] = None
    align_with_previous: Optional[bool] = None


# ============== Storyboard Schemas ==============

class Storyboard(BaseModel):
    scene_overview: str
    shots: List[ShotItem]


class StoryboardUpdate(BaseModel):
    scene_overview: Optional[str] = None
    shots: Optional[List[ShotItem]] = None


class StoryboardReplace(BaseModel):
    """Full-replace storyboard: both fields required (vs StoryboardUpdate's optionals)."""
    scene_overview: str
    shots: List[ShotItem] = Field(..., min_length=1)

    @field_validator("shots")
    @classmethod
    def _unique_shot_ids(cls, v: List[ShotItem]) -> List[ShotItem]:
        ids = [s.shot_id for s in v]
        if len(ids) != len(set(ids)):
            raise ValueError("shot_id values must be unique")
        return v


# ============== Project Schemas ==============

class ProjectCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    theme_text: str = Field(..., min_length=1, max_length=1000)
    aspect_ratio: str = Field(default="9:16", pattern="^(16:9|9:16)$")


class ReferenceVoiceRequest(BaseModel):
    shot_id: int


class AutoVoiceCalibrateRequest(BaseModel):
    enabled: bool


class ProjectResponse(BaseModel):
    id: str
    title: str
    theme_text: str
    aspect_ratio: str = "9:16"
    creator_name: str
    status: str
    scene_overview: Optional[str] = None
    storyboard_path: Optional[str] = None
    final_video_path: Optional[str] = None
    error_message: Optional[str] = None
    reference_voice_shot_id: Optional[int] = None
    reference_voice_path: Optional[str] = None
    auto_voice_calibrate: bool = False
    created_at: datetime
    updated_at: datetime
    reference_images: List[ReferenceImageResponse] = []
    shots: List[ShotResponse] = []
    storyboard: Optional[Storyboard] = None

    class Config:
        from_attributes = True


class ProjectListResponse(BaseModel):
    id: str
    title: str
    theme_text: str
    aspect_ratio: str = "9:16"
    creator_name: str
    status: str
    scene_overview: Optional[str] = None
    final_video_path: Optional[str] = None
    error_message: Optional[str] = None
    reference_voice_shot_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    shot_count: int = 0
    completed_shot_count: int = 0

    class Config:
        from_attributes = True


class ProjectList(BaseModel):
    items: List[ProjectListResponse]
    total: int
    limit: int
    offset: int


# ============== Pipeline Action Schemas ==============

class RegenerateShotsRequest(BaseModel):
    shot_ids: List[int]


class ExportRequest(BaseModel):
    crossfade_duration: Optional[float] = Field(default=None, ge=0, le=2.0)


class JoinPreviewRequest(BaseModel):
    shot_ids: list[int]


class PipelineActionResponse(BaseModel):
    success: bool
    message: str
    new_status: Optional[str] = None


# ============== SSE Event Schemas ==============

class StateSnapshotEvent(BaseModel):
    type: str = "state_snapshot"
    status: str
    shots: List[ShotResponse]
    storyboard: Optional[Storyboard] = None


class StateChangeEvent(BaseModel):
    type: str = "state_change"
    from_status: str
    to_status: str


class ScriptReadyEvent(BaseModel):
    type: str = "script_ready"
    storyboard: Storyboard


class ShotStartedEvent(BaseModel):
    type: str = "shot_started"
    shot_id: int


class ShotProgressEvent(BaseModel):
    type: str = "shot_progress"
    shot_id: int
    sub_status: str


class ShotCompletedEvent(BaseModel):
    type: str = "shot_completed"
    shot_id: int
    preview_url: Optional[str] = None
    video_url: Optional[str] = None


class ShotFailedEvent(BaseModel):
    type: str = "shot_failed"
    shot_id: int
    error: str


class AllShotsReadyEvent(BaseModel):
    type: str = "all_shots_ready"
    has_failures: bool


class ExportDoneEvent(BaseModel):
    type: str = "export_done"
    download_url: str


class PipelineFailedEvent(BaseModel):
    type: str = "pipeline_failed"
    reason: str


# ============== Error Schema ==============

class ErrorResponse(BaseModel):
    error: dict


# ============== Health Check ==============

class HealthResponse(BaseModel):
    status: str
    redis: str
    db: str
