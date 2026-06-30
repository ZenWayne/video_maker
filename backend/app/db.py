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
        await _run_migrations(conn)


async def _run_migrations(conn):
    """Run idempotent migrations for columns not handled by create_all."""
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

    if not await _has_column("shots", "auto_trim"):
        await conn.execute(
            sa.text("ALTER TABLE shots ADD COLUMN auto_trim BOOLEAN NOT NULL DEFAULT 1")
        )

    for col, typ in [
        ("trim_frames", "INTEGER"),
        ("source_fps", "FLOAT"),
        ("source_frames", "INTEGER"),
        ("vc_audio_path", "TEXT"),
    ]:
        if not await _has_column("shots", col):
            await conn.execute(sa.text(f"ALTER TABLE shots ADD COLUMN {col} {typ}"))
