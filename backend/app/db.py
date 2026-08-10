"""Database connection and session management."""

from pathlib import Path

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


async def assert_migrations_current(target_engine) -> None:
    """校验数据库已升级到 Alembic head，否则抛 RuntimeError。

    迁移到 Alembic 之后，表结构不再由应用启动时创建。若库落后于代码，
    我们要在启动瞬间就明确失败，而不是等到第一个查询报 UndefinedColumn。
    """
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    backend_dir = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    expected = set(ScriptDirectory.from_config(cfg).get_heads())

    def _current_heads(sync_conn) -> set:
        return set(MigrationContext.configure(sync_conn).get_current_heads())

    async with target_engine.connect() as conn:
        actual = await conn.run_sync(_current_heads)

    if actual != expected:
        raise RuntimeError(
            f"数据库表结构版本不匹配：库在 {actual or '{}'}，代码要求 {expected}。"
            f"请先运行 `alembic upgrade head` 再启动服务。"
        )


async def init_db():
    """启动检查。**不再建表** —— 表结构由 Alembic 管理。"""
    await assert_migrations_current(engine)
