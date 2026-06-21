"""SQLAlchemy ORM models for projects, shots, reference images, and events."""

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Column,
    String,
    Text,
    Integer,
    Boolean,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class ProjectStatus(str, Enum):
    DRAFT = "draft"
    SCRIPTING = "scripting"
    SCRIPT_REVIEW = "script_review"
    SHOT_GENERATING = "shot_generating"
    SHOT_REVIEW = "shot_review"
    EXPORTING = "exporting"
    EXPORTED = "exported"
    FAILED = "failed"


class ShotStatus(str, Enum):
    PENDING = "pending"
    PROMPT_GENERATING = "prompt_generating"
    VIDEO_GENERATING = "video_generating"
    COMPLETED = "completed"
    FAILED = "failed"


class ReferenceImageKind(str, Enum):
    CHARACTER = "character"
    SCENE = "scene"


class Project(Base):
    __tablename__ = "projects"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(Text, nullable=False)
    theme_text = Column(Text, nullable=False)
    creator_name = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default=ProjectStatus.DRAFT.value)
    aspect_ratio = Column(String(10), nullable=False, default="16:9")
    scene_overview = Column(Text, nullable=True)
    storyboard_path = Column(Text, nullable=True)
    final_video_path = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    reference_voice_shot_id = Column(Integer, nullable=True)  # shot_id of reference voice
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    reference_images = relationship(
        "ReferenceImage",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ReferenceImage.order_index",
    )
    shots = relationship(
        "Shot",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="Shot.shot_id",
    )
    events = relationship(
        "Event",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="Event.created_at.desc()",
    )

    __table_args__ = (
        Index("ix_projects_status", "status"),
        Index("ix_projects_creator_name", "creator_name"),
        Index("ix_projects_created_at", "created_at"),
    )


class ReferenceImage(Base):
    __tablename__ = "reference_images"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind = Column(String(20), nullable=False)  # 'character' or 'scene'
    filename = Column(Text, nullable=False)
    storage_path = Column(Text, nullable=False)
    order_index = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    project = relationship("Project", back_populates="reference_images")

    __table_args__ = (
        Index("ix_ref_images_project_kind_order", "project_id", "kind", "order_index"),
    )


class Shot(Base):
    __tablename__ = "shots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    shot_id = Column(Integer, nullable=False)  # Sequence number starting from 1
    text = Column(Text, nullable=False)  # Dialogue/text
    shot_type = Column(String(50), nullable=False)  # Close-up, Medium Shot, Wide Shot
    visual_description = Column(Text, nullable=False)
    shot_duration = Column(Integer, nullable=False)  # 4, 6, or 8 seconds
    status = Column(String(30), nullable=False, default=ShotStatus.PENDING.value)
    align_with_previous = Column(Boolean, nullable=False, default=True)
    use_prev_last_frame = Column(Boolean, nullable=False, default=True)
    motion_prompt = Column(Text, nullable=True)
    first_frame_path = Column(Text, nullable=True)
    video_path = Column(Text, nullable=True)
    last_frame_path = Column(Text, nullable=True)
    veo_operation_id = Column(Text, nullable=True)
    word_count_warning = Column(Boolean, default=False)
    error_message = Column(Text, nullable=True)
    custom_first_frame_path = Column(Text, nullable=True)  # 用户上传的自定义首帧
    custom_reference_paths = Column(Text, nullable=True)  # JSON: ["path1.png","path2.png"]
    reference_image_hint = Column(Text, nullable=True)  # AI 生成的参考图上传提示
    vc_status = Column(String(20), nullable=True)  # null | "converting" | "done" | "failed"
    vc_error_message = Column(Text, nullable=True)
    cc_status = Column(String(20), nullable=True)  # null | "calibrating" | "done" | "failed"
    cc_error_message = Column(Text, nullable=True)
    skip_tail_frame = Column(Boolean, default=False)  # 用户选择跳过尾帧，只用首帧生成
    target_last_frame_path = Column(Text, nullable=True)  # AI 生成的目标尾帧
    tf_status = Column(String(20), nullable=True)  # null | "generating" | "done" | "failed"
    tf_error_message = Column(Text, nullable=True)
    tf_confirmed = Column(Boolean, default=False)  # 用户已确认尾帧
    auto_trim = Column(Boolean, nullable=False, default=True)  # 生成后自动 SSIM 裁剪
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    project = relationship("Project", back_populates="shots")

    __table_args__ = (
        UniqueConstraint("project_id", "shot_id", name="uq_shot_project_shot_id"),
        Index("ix_shots_project_shot_id", "project_id", "shot_id"),
        Index("ix_shots_project_status", "project_id", "status"),
    )


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor = Column(Text, nullable=False)  # 'user:{name}' or 'system:worker'
    event_type = Column(Text, nullable=False)
    payload = Column(Text, nullable=True)  # JSON string
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    project = relationship("Project", back_populates="events")

    __table_args__ = (
        Index("ix_events_project_created", "project_id", "created_at"),
    )
