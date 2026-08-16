"""把存量数据从 SQLite 全量搬到 PostgreSQL。

用法（在 backend/ 下）：

    uv run --project . python -m app.scripts.pg_migration.migrate \\
        --src sqlite+aiosqlite:////app/data/dev.db \\
        --dst postgresql+asyncpg://videomaker:PASS@localhost:5433/videomaker

前置条件：目标库已 `alembic upgrade head`（表结构必须先在）。
本脚本只插数据，不建表。
"""

import argparse
import asyncio

from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.models.project import Base

# 外键依赖顺序：父表必须先于子表。
# content_analyses 独立于 projects，但 projects.content_analysis_id 指向它，
# 所以放在最前面最安全。
TABLE_ORDER: tuple[str, ...] = (
    "content_analyses",
    "reference_samples",
    "projects",
    "shots",
    "reference_images",
    "image_candidates",
    "events",
)

# 用 SERIAL/identity 做主键的表 —— 搬完必须对齐序列
_SEQUENCE_TABLES: tuple[tuple[str, str], ...] = (
    ("shots", "id"),
    ("events", "id"),
    ("reference_samples", "id"),
)


async def copy_all(src_url: str, dst_url: str) -> dict[str, int]:
    """按外键顺序全量搬迁，返回每张表搬了多少行。"""
    src = create_async_engine(src_url, poolclass=NullPool)
    dst = create_async_engine(dst_url, poolclass=NullPool)
    counts: dict[str, int] = {}
    try:
        for name in TABLE_ORDER:
            table = Base.metadata.tables[name]
            async with src.connect() as sconn:
                rows = [dict(r) for r in (await sconn.execute(select(table))).mappings()]
            if rows:
                async with dst.begin() as dconn:
                    await dconn.execute(insert(table), rows)
            counts[name] = len(rows)

        # 对齐自增序列：不做的话下一次 INSERT 会从 1 开始，撞上已搬入的主键。
        async with dst.begin() as dconn:
            for table_name, pk in _SEQUENCE_TABLES:
                await dconn.execute(text(
                    f"SELECT setval("
                    f"  pg_get_serial_sequence('{table_name}', '{pk}'),"
                    f"  COALESCE((SELECT MAX({pk}) FROM {table_name}), 1),"
                    f"  (SELECT MAX({pk}) IS NOT NULL FROM {table_name})"
                    f")"
                ))
    finally:
        await src.dispose()
        await dst.dispose()
    return counts


async def verify(src_url: str, dst_url: str) -> dict[str, tuple[int, int]]:
    """逐表比对源库与目标库的行数，返回 {表名: (源, 目标)}。"""
    src = create_async_engine(src_url, poolclass=NullPool)
    dst = create_async_engine(dst_url, poolclass=NullPool)
    result: dict[str, tuple[int, int]] = {}
    try:
        for name in TABLE_ORDER:
            table = Base.metadata.tables[name]
            stmt = select(func.count()).select_from(table)
            async with src.connect() as c:
                s_n = (await c.execute(stmt)).scalar_one()
            async with dst.connect() as c:
                d_n = (await c.execute(stmt)).scalar_one()
            result[name] = (s_n, d_n)
    finally:
        await src.dispose()
        await dst.dispose()
    return result


async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="源 SQLite URL")
    ap.add_argument("--dst", required=True, help="目标 PostgreSQL URL")
    args = ap.parse_args()

    counts = await copy_all(args.src, args.dst)
    for name, n in counts.items():
        print(f"  搬迁 {name}: {n} 行")

    print("\n逐表比对：")
    mismatched = False
    for name, (s_n, d_n) in (await verify(args.src, args.dst)).items():
        flag = "OK" if s_n == d_n else "不一致"
        if s_n != d_n:
            mismatched = True
        print(f"  {name}: 源={s_n} 目标={d_n} [{flag}]")

    if mismatched:
        raise SystemExit("搬迁后行数不一致，请勿切流量")
    print("\n全部一致。")


if __name__ == "__main__":
    asyncio.run(_main())
