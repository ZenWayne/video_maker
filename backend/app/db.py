"""Database connection and session management."""

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.config import settings

# SQLite with aiosqlite doesn't benefit from connection pooling — each
# aiosqlite connection is an independent async process. NullPool creates a
# fresh connection per session and closes it immediately on release, which
# eliminates QueuePool exhaustion under concurrent SSE streams.
_pool_kwargs: dict = {}
if settings.database_url.startswith("sqlite"):
    from sqlalchemy.pool import NullPool
    _pool_kwargs["poolclass"] = NullPool

# Create async engine
engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    **_pool_kwargs,
)

# Create async session factory
AsyncSession = async_sessionmaker(
    engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session():
    """Dependency for FastAPI to get database session."""
    async with AsyncSession() as session:
        yield session


async def init_db():
    """Initialize database tables."""
    from app.models.project import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_columns(conn)


async def _ensure_columns(conn) -> None:
    """幂等建列。启动时调用；Spec B 的迁移脚本也会直接调用以保证回填前列已存在。"""
    import sqlalchemy as sa

    # Helper to check if a column exists
    async def _has_column(table: str, column: str) -> bool:
        result = await conn.execute(sa.text(f"PRAGMA table_info({table})"))
        return column in {row[1] for row in result}

    if not await _has_column("projects", "aspect_ratio"):
        await conn.execute(
            sa.text("ALTER TABLE projects ADD COLUMN aspect_ratio VARCHAR(10) NOT NULL DEFAULT '16:9'")
        )
    if not await _has_column("shots", "reference_image_hint"):
        await conn.execute(
            sa.text("ALTER TABLE shots ADD COLUMN reference_image_hint TEXT")
        )
    if not await _has_column("projects", "reference_voice_shot_id"):
        await conn.execute(
            sa.text("ALTER TABLE projects ADD COLUMN reference_voice_shot_id INTEGER")
        )
    if not await _has_column("projects", "reference_voice_path"):
        await conn.execute(
            sa.text("ALTER TABLE projects ADD COLUMN reference_voice_path TEXT")
        )
    if not await _has_column("projects", "auto_voice_calibrate"):
        await conn.execute(
            sa.text("ALTER TABLE projects ADD COLUMN auto_voice_calibrate BOOLEAN NOT NULL DEFAULT 0")
        )
    if not await _has_column("shots", "vc_status"):
        await conn.execute(
            sa.text("ALTER TABLE shots ADD COLUMN vc_status VARCHAR(20)")
        )
    if not await _has_column("shots", "vc_error_message"):
        await conn.execute(
            sa.text("ALTER TABLE shots ADD COLUMN vc_error_message TEXT")
        )
    if not await _has_column("shots", "cc_status"):
        await conn.execute(
            sa.text("ALTER TABLE shots ADD COLUMN cc_status VARCHAR(20)")
        )
    if not await _has_column("shots", "cc_error_message"):
        await conn.execute(
            sa.text("ALTER TABLE shots ADD COLUMN cc_error_message TEXT")
        )
    for col, typ in [
        ("target_last_frame_path", "TEXT"),
        ("tf_status", "VARCHAR(20)"),
        ("tf_error_message", "TEXT"),
        ("tf_confirmed", "BOOLEAN DEFAULT 0"),
    ]:
        if not await _has_column("shots", col):
            await conn.execute(sa.text(f"ALTER TABLE shots ADD COLUMN {col} {typ}"))

    # skip_tail_frame removed (path-as-truth): a tail frame is used iff
    # target_last_frame_path is set. Drop the now-dead column if present.
    if await _has_column("shots", "skip_tail_frame"):
        await conn.execute(
            sa.text("ALTER TABLE shots DROP COLUMN skip_tail_frame")
        )

    # first_frame_path removed (single-source): custom_first_frame_path is the ONLY
    # stored first-frame field; the frame fed to the model is resolved on demand by
    # services.first_frame.pick_first_frame. The old persisted "resolved" copy was a
    # cache that went stale when a 首帧 was re-uploaded. Drop the dead column.
    if await _has_column("shots", "first_frame_path"):
        await conn.execute(
            sa.text("ALTER TABLE shots DROP COLUMN first_frame_path")
        )

    if not await _has_column("shots", "auto_trim"):
        await conn.execute(
            sa.text("ALTER TABLE shots ADD COLUMN auto_trim BOOLEAN NOT NULL DEFAULT 1")
        )

    for col, typ in [
        ("trim_frames", "INTEGER"),
        ("source_fps", "FLOAT"),
        ("source_frames", "INTEGER"),
        ("vc_audio_path", "TEXT"),
        ("audio_head_mute_frames", "INTEGER"),
        ("ff_status", "VARCHAR(20)"),
        ("ff_error_message", "TEXT"),
    ]:
        if not await _has_column("shots", col):
            await conn.execute(sa.text(f"ALTER TABLE shots ADD COLUMN {col} {typ}"))

    for col, typ in [
        ("pre_cc_last_frame_key", "TEXT"),
        ("pristine_last_frame_key", "TEXT"),
    ]:
        if not await _has_column("shots", col):
            await conn.execute(sa.text(f"ALTER TABLE shots ADD COLUMN {col} {typ}"))

    # pre_vc_video_key 是非破坏式 VC 改造后的死功能：写入方 ensure_pre_vc_backup
    # 已删除，且从来没有任何还原路径读它（voice-revert 只清 vc_audio_path）。
    # 存量库里可能已建出该列，这里幂等删掉。
    if await _has_column("shots", "pre_vc_video_key"):
        await conn.execute(sa.text("ALTER TABLE shots DROP COLUMN pre_vc_video_key"))

    for col, typ in [
        ("content_analysis_id", "VARCHAR(36)"),
        ("attached_brief_json", "TEXT"),
    ]:
        if not await _has_column("projects", col):
            await conn.execute(sa.text(f"ALTER TABLE projects ADD COLUMN {col} {typ}"))

    # 归属字段（应用级鉴权 FR-8.3）。create_all 只建缺失的**表**，不会给已存在
    # 的 projects 表加列，所以新列必须在这里补。留 NULL 表示「鉴权上线前的存量
    # 数据」，由 P3 的回填脚本指向 stella；ALTER TABLE 不能加 REFERENCES，外键
    # 约束只对新建库（create_all 路径）生效。
    if not await _has_column("projects", "owner_id"):
        await conn.execute(sa.text("ALTER TABLE projects ADD COLUMN owner_id VARCHAR(36)"))
        await conn.execute(
            sa.text("CREATE INDEX IF NOT EXISTS ix_projects_owner_id ON projects (owner_id)")
        )
