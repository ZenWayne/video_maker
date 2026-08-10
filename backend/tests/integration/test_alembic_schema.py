"""Alembic 的 initial revision 必须与 ORM 模型完全一致。

这是本期最重要的一道防线：一旦 revision 与模型漂移，应用会连上一个
「看起来能跑、但少一列」的库，故障会推迟到运行时才暴露。
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text

from app.models.project import Base

BACKEND_DIR = Path(__file__).resolve().parents[2]
EXPECTED_TABLES = {
    "projects",
    "shots",
    "reference_images",
    "image_candidates",
    "events",
    "content_analyses",
    "reference_samples",
}


def _sync_test_url() -> str:
    """Alembic 的 command API 是同步的，这里要 psycopg 风格的同步 URL。"""
    return os.environ.get(
        "TEST_DATABASE_URL_SYNC",
        "postgresql://videomaker:devpassword@localhost:5433/videomaker_test",
    )


@pytest.fixture
def alembic_config():
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", _sync_test_url())
    return cfg


@pytest.fixture
def clean_db(alembic_config):
    """每个测试从一个干净的 public schema 开始。"""
    engine = create_engine(_sync_test_url())
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


def test_upgrade_head_creates_all_tables(alembic_config, clean_db):
    command.upgrade(alembic_config, "head")
    with clean_db.connect() as conn:
        rows = conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )).scalars().all()
    assert EXPECTED_TABLES.issubset(set(rows))


def test_head_schema_matches_orm_models(alembic_config, clean_db):
    """upgrade 到 head 后，autogenerate 应当检测不出任何差异。"""
    command.upgrade(alembic_config, "head")
    with clean_db.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == [], f"Alembic revision 与 ORM 模型不一致：{diff}"
