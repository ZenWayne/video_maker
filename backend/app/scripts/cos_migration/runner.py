"""存量迁移四阶段：scan / upload / backfill / verify。

设计要点：
- 四阶段全部幂等，可中断重跑（Spec B §2.1 的硬要求：连跑两次，
  第二次变更数必须为 0）。
- ``key_prefix`` 在**对象存储边界**统一生效：生产用 ""，测试传
  cos_prefix，好让集成测试写进独立前缀、teardown 能删干净。DB 里存的
  永远是不带前缀的 key。历史上正是因为测试直接写真实 projects/ 前缀
  且无 teardown，dev bucket 才积了 2893 个孤儿对象。
- 本模块只依赖 app.models / app.services / app.db，**绝不 import
  app.api.* 或 app.main**（worker 侧同源禁忌，见 CLAUDE.md）。
"""

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import sqlalchemy as sa

from app.scripts.cos_migration.fields import (
    ALREADY_KEY, LEGACY_ABS, LEGACY_REL, UNRECOGNIZED,
    FIELDS, classify, rewrite_json_dict_of_lists, rewrite_json_list, to_key,
)

logger = logging.getLogger(__name__)


@dataclass
class DbRef:
    table: str
    column: str
    pk: str
    raw: str
    key: Optional[str]
    kind: str


async def _table_exists(conn, table: str) -> bool:
    r = await conn.execute(sa.text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=:t"), {"t": table})
    return r.first() is not None


def _refs_from_value(spec, pk, raw: str) -> list[DbRef]:
    """把一个字段值展开成 0..n 条引用（JSON 字段会展开成多条）。"""
    out = []
    if spec.kind == "scalar":
        kind = classify(raw)
        key = to_key(raw) if kind != UNRECOGNIZED else None
        out.append(DbRef(spec.table, spec.column, str(pk), raw, key, kind))
        return out
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return out
    items = []
    if spec.kind == "json_list" and isinstance(data, list):
        items = [x for x in data if isinstance(x, str)]
    elif spec.kind == "json_dict_of_lists" and isinstance(data, dict):
        items = [x for v in data.values() if isinstance(v, list) for x in v if isinstance(x, str)]
    for it in items:
        kind = classify(it)
        key = to_key(it) if kind != UNRECOGNIZED else None
        out.append(DbRef(spec.table, spec.column, str(pk), it, key, kind))
    return out


async def collect_db_refs(session_factory) -> list[DbRef]:
    """遍历 FIELDS，收集全部非空路径值并分类。"""
    refs: list[DbRef] = []
    async with session_factory() as s:
        conn = await s.connection()
        for spec in FIELDS:
            if spec.optional_table and not await _table_exists(conn, spec.table):
                logger.info("skip_missing_table", extra={"table": spec.table})
                continue
            rows = await s.execute(sa.text(
                f"SELECT {spec.pk}, {spec.column} FROM {spec.table} "
                f"WHERE {spec.column} IS NOT NULL AND {spec.column} <> ''"))
            for pk, raw in rows.fetchall():
                refs.extend(_refs_from_value(spec, pk, raw))
    return refs


def _walk_local(storage_root: Path) -> dict[str, int]:
    """storage_root 下所有文件 → {相对路径(=key): 字节数}。"""
    out = {}
    for p in storage_root.rglob("*"):
        if p.is_file():
            out[p.relative_to(storage_root).as_posix()] = p.stat().st_size
    return out


async def scan(storage_root: Path, session_factory) -> dict:
    """扫描本地 storage 与 DB，产出迁移清单 + 悬空基线 + 未引用文件报告。

    悬空基线是 Spec B 没有的概念，但现实需要：实测 312 条媒体引用里有
    200 条的本地文件早已不存在（55 个项目目录整体消失）。这是迁移**之前**
    就存在的破损，迁移无法修复。把它们固化成基线，--verify 才能对
    「基线之外的缺失」判失败而不是永远飘红。
    """
    storage_root = Path(storage_root)
    local = _walk_local(storage_root)
    refs = await collect_db_refs(session_factory)

    counts = {ALREADY_KEY: 0, LEGACY_REL: 0, LEGACY_ABS: 0, UNRECOGNIZED: 0}
    dangling, unrecognized, referenced = [], [], set()
    for r in refs:
        counts[r.kind] += 1
        if r.key is None:
            unrecognized.append({"table": r.table, "column": r.column,
                                 "pk": r.pk, "raw": r.raw})
            continue
        referenced.add(r.key)
        if r.key not in local:
            dangling.append(asdict(r))

    unref = {k: v for k, v in local.items() if k not in referenced}
    return {
        "phase": "scan",
        "local": {"files": len(local), "bytes": sum(local.values())},
        "db": {"total": len(refs), **{k: counts[k] for k in counts}},
        "dangling": dangling,
        "unreferenced_local": {
            "files": len(unref), "bytes": sum(unref.values()),
            "sample": sorted(unref)[:50],
        },
        "unrecognized": unrecognized,
    }


def write_report(report: dict, report_dir: Path, name: str) -> Path:
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{name}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
