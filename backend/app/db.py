"""Database connection and session management."""

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.config import settings


def build_pool_kwargs(database_url: str) -> dict:
    """按数据库后端选择连接池策略。

    SQLite + aiosqlite：每个连接是独立的异步进程，池化无收益；而 QueuePool
    在 SSE 长连接并发时会被耗尽。用 NullPool——每次会话新建连接、释放即关。

    PostgreSQL：建连接昂贵，必须池化。pool_pre_ping 让被中间件掐掉的死连接
    在使用前被发现并重建；pool_recycle 避免连接活得比服务端 idle 超时更久。
    """
    if database_url.startswith("sqlite"):
        from sqlalchemy.pool import NullPool
        return {"poolclass": NullPool}
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_pre_ping": True,
        "pool_recycle": settings.db_pool_recycle_sec,
    }


_database_url = settings.resolved_database_url

# Create async engine
engine = create_async_engine(
    _database_url,
    echo=False,
    future=True,
    **build_pool_kwargs(_database_url),
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
