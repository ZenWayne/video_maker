"""三个素材状态列：建列幂等 + 可读写。"""
import sqlalchemy as sa
from sqlalchemy import select

from app.models.project import Shot

from tests.integration.conftest import _make_project, _add_shot


async def test_columns_exist_on_fresh_schema(db_engine):
    async with db_engine.begin() as conn:
        cols = await conn.run_sync(
            lambda c: [r[1] for r in c.exec_driver_sql("PRAGMA table_info(shots)")]
        )
    assert "pre_cc_last_frame_key" in cols
    assert "pristine_last_frame_key" in cols


async def test_columns_default_to_null_and_are_writable(db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        assert shot.pre_cc_last_frame_key is None
        assert shot.pristine_last_frame_key is None

        shot.pristine_last_frame_key = "projects/p/shots/shot_1/last_frame_1_ab.png"
        await s.commit()

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        assert shot.pristine_last_frame_key.endswith("last_frame_1_ab.png")


async def test_migration_is_idempotent_on_legacy_table(db_engine):
    """在缺列的旧表上重复执行建列例程，不应报错。"""
    from app.db import _ensure_columns  # Step 3 中新增的可复用例程

    async with db_engine.begin() as conn:
        await conn.execute(sa.text("ALTER TABLE shots DROP COLUMN pre_cc_last_frame_key"))

    async with db_engine.begin() as conn:
        await _ensure_columns(conn)
        await _ensure_columns(conn)  # 第二次必须无害

    async with db_engine.begin() as conn:
        cols = await conn.run_sync(
            lambda c: [r[1] for r in c.exec_driver_sql("PRAGMA table_info(shots)")]
        )
    assert "pre_cc_last_frame_key" in cols


async def test_pre_vc_video_key_column_is_gone(db_engine):
    """pre_vc_video_key 是非破坏式改造后的死功能（没有任何还原路径读它），
    已随 ensure_pre_vc_backup 一并删除。db.py 的幂等 DROP 必须把存量库里
    的这一列也清掉。"""
    async with db_engine.begin() as conn:
        cols = [r[1] for r in (await conn.execute(sa.text("PRAGMA table_info(shots)"))).fetchall()]
    assert "pre_vc_video_key" not in cols
    # 另外两列必须岿然不动
    assert "pre_cc_last_frame_key" in cols
    assert "pristine_last_frame_key" in cols
