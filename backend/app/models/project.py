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
    Float,
    Boolean,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    Index,
    text,
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


class ContentAnalysisStatus(str, Enum):
    UPLOADING = "uploading"
    TRANSCRIBING = "transcribing"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    FAILED = "failed"


class ReferenceSampleStatus(str, Enum):
    PENDING = "pending"
    TRANSCRIBING = "transcribing"
    TRANSCRIBED = "transcribed"
    FAILED = "failed"


class CreditReason(str, Enum):
    REGISTER = "register"
    GRANT = "grant"
    RESERVE = "reserve"
    REFUND = "refund"


class User(Base):
    """An application account.

    Accounts live in the DB (not in a secret): self-service registration at a
    scale of ≤1000 accounts makes re-rendering a secret + restarting pods per
    signup untenable. Only the *machine token* stays in secrets.

    ``credits`` is the authoritative balance; ``credit_ledger`` is the audit
    trail and the basis for refunds (see app.services.credits).
    """

    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String(64), nullable=False, unique=True)
    password_hash = Column(Text, nullable=False)
    credits = Column(Integer, nullable=False, default=0)
    is_admin = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_users_username", "username", unique=True),
    )


class CreditLedger(Base):
    """Append-only credit movements.

    Every balance change writes exactly one row **in the same transaction** as
    the ``users.credits`` update — otherwise a deduction can exist with no
    traceable origin, or a refund can run twice.

    ``ref_type``/``ref_id`` point at what caused the movement; for a refund
    they point at the reserve row being refunded (``ref_type='reservation'``),
    which is what makes refunds idempotent via ``uq_credit_ledger_refund``.
    """

    __tablename__ = "credit_ledger"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    delta = Column(Integer, nullable=False)  # negative = deduction
    reason = Column(String(20), nullable=False)  # register|grant|reserve|refund
    ref_type = Column(String(40), nullable=True)
    ref_id = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_credit_ledger_user_created", "user_id", "created_at"),
        # One refund per reservation, enforced by the DB rather than by a
        # check-then-insert race in the worker.
        Index(
            "uq_credit_ledger_refund",
            "ref_type",
            "ref_id",
            unique=True,
            sqlite_where=text("reason = 'refund'"),
        ),
    )


class Project(Base):
    __tablename__ = "projects"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(Text, nullable=False)
    theme_text = Column(Text, nullable=False)
    # Display name only. Access control is decided by owner_id — never by this
    # field, or renaming yourself would be a privilege escalation.
    creator_name = Column(Text, nullable=False)
    # Authoritative owner. Nullable for pre-auth rows until the FR-8.3
    # migration (P3) backfills them.
    owner_id = Column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    status = Column(String(20), nullable=False, default=ProjectStatus.DRAFT.value)
    aspect_ratio = Column(String(10), nullable=False, default="9:16")
    scene_overview = Column(Text, nullable=True)
    storyboard_path = Column(Text, nullable=True)
    final_video_path = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    reference_voice_shot_id = Column(Integer, nullable=True)  # shot_id of reference voice
    reference_voice_path = Column(Text, nullable=True)  # uploaded base-voice prompt.wav (file source)
    auto_voice_calibrate = Column(Boolean, nullable=False, default=False)  # auto-run VC after video gen
    content_analysis_id = Column(String(36), nullable=True)  # 溯源：挂载的分析 id
    attached_brief_json = Column(Text, nullable=True)         # brief 快照
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
        Index("ix_projects_owner_id", "owner_id"),
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
    video_path = Column(Text, nullable=True)
    last_frame_path = Column(Text, nullable=True)
    veo_operation_id = Column(Text, nullable=True)
    word_count_warning = Column(Boolean, default=False)
    error_message = Column(Text, nullable=True)
    custom_first_frame_path = Column(Text, nullable=True)  # 用户上传的自定义首帧
    ff_status = Column(String(20), nullable=True)  # null | "generating" | "done" | "failed"
    ff_error_message = Column(Text, nullable=True)
    custom_reference_paths = Column(Text, nullable=True)  # JSON: ["path1.png","path2.png"]
    reference_image_hint = Column(Text, nullable=True)  # AI 生成的参考图上传提示
    vc_status = Column(String(20), nullable=True)  # null | "converting" | "done" | "failed"
    vc_error_message = Column(Text, nullable=True)
    cc_status = Column(String(20), nullable=True)  # null | "calibrating" | "done" | "failed"
    cc_error_message = Column(Text, nullable=True)
    target_last_frame_path = Column(Text, nullable=True)  # AI 生成的目标尾帧
    tf_status = Column(String(20), nullable=True)  # null | "generating" | "done" | "failed"
    tf_error_message = Column(Text, nullable=True)
    tf_confirmed = Column(Boolean, default=False)  # 用户已确认尾帧
    auto_trim = Column(Boolean, nullable=False, default=True)  # 生成后自动 SSIM 裁剪
    # --- 非破坏式编辑 EDL ---
    trim_frames = Column(Integer, nullable=True)      # 从头保留帧数；None=不裁剪
    source_fps = Column(Float, nullable=True)         # 源视频 fps（生成时写入）
    source_frames = Column(Integer, nullable=True)    # 源视频总帧数
    vc_audio_path = Column(Text, nullable=True)       # 替换音轨 wav；None=用源原音
    audio_head_mute_frames = Column(Integer, nullable=True)  # 前 [0,N) 帧静音；None/0=不静音
    # ── 素材状态显式化（原先靠目录扫描/固定文件名推导，COS 下不成立）──────
    # 角色校准前的尾帧备份 key。取代「last_frame_pre_cc.png 是否存在」。
    pre_cc_last_frame_key = Column(Text, nullable=True)
    # 未经校准的原始尾帧 key（CC 还原目标）。
    # 必需：CC 会直接覆盖 last_frame_path，校准后无法反推校准前的尾帧。
    pristine_last_frame_key = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    project = relationship("Project", back_populates="shots")
    image_candidates = relationship(
        "ImageCandidate",
        back_populates="shot",
        cascade="all, delete-orphan",
        order_by="ImageCandidate.created_at",
        lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint("project_id", "shot_id", name="uq_shot_project_shot_id"),
        Index("ix_shots_project_shot_id", "project_id", "shot_id"),
        Index("ix_shots_project_status", "project_id", "status"),
    )


class ImageCandidate(Base):
    __tablename__ = "image_candidates"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    shot_pk = Column(
        Integer,
        ForeignKey("shots.id", ondelete="CASCADE"),
        nullable=False,
    )
    shot_id = Column(Integer, nullable=False)  # Shot.shot_id 序号（冗余便于查询/事件）
    slot = Column(String(20), nullable=False)  # 'first_frame' | 'tail_frame' | 'cc'
    status = Column(String(20), nullable=False, default="generating")  # generating|done|failed
    file_path = Column(Text, nullable=True)
    prompt_source = Column(String(10), nullable=False, default="auto")  # auto|custom
    custom_prompt = Column(Text, nullable=True)
    ref_paths = Column(Text, nullable=True)  # JSON: {"character": [...], "object": [...]}
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    adopted_at = Column(DateTime, nullable=True)

    shot = relationship("Shot", back_populates="image_candidates")

    __table_args__ = (
        Index("ix_image_candidates_shot", "project_id", "shot_id"),
    )


class ContentAnalysis(Base):
    __tablename__ = "content_analyses"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(Text, nullable=False)
    region_hint = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default=ContentAnalysisStatus.UPLOADING.value)
    brief_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    samples = relationship(
        "ReferenceSample",
        back_populates="analysis",
        cascade="all, delete-orphan",
        order_by="ReferenceSample.order_index",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_content_analyses_status", "status"),
        Index("ix_content_analyses_created_at", "created_at"),
    )


class ReferenceSample(Base):
    __tablename__ = "reference_samples"

    id = Column(Integer, primary_key=True, autoincrement=True)
    analysis_id = Column(
        String(36),
        ForeignKey("content_analyses.id", ondelete="CASCADE"),
        nullable=False,
    )
    order_index = Column(Integer, nullable=False, default=0)
    video_path = Column(Text, nullable=False)
    audio_path = Column(Text, nullable=True)
    has_speech = Column(Boolean, nullable=True)
    hook_text = Column(Text, nullable=True)
    full_transcript = Column(Text, nullable=True)
    language = Column(String(10), nullable=True)
    status = Column(String(20), nullable=False, default=ReferenceSampleStatus.PENDING.value)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    analysis = relationship("ContentAnalysis", back_populates="samples")

    __table_args__ = (
        Index("ix_reference_samples_analysis", "analysis_id"),
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
