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

from app.services import cos_client, object_store
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


async def upload(storage_root: Path, key_prefix: str = "",
                 only: Optional[list[str]] = None) -> dict:
    """把本地 storage 下的文件传到 COS。幂等：已存在且字节数一致则跳过。

    ``only`` 限定只处理给定的 key 列表（用于按失败清单重试，以及测试里
    模拟「传到一半中断」）。

    可在线运行、不动 DB —— 这是切换窗口能压到极短的原因（Spec B §3.1
    第 2 步在线消化掉大头）。
    """
    storage_root = Path(storage_root)
    await cos_client.warm_credentials()
    local = _walk_local(storage_root)
    if only is not None:
        wanted = set(only)
        local = {k: v for k, v in local.items() if k in wanted}

    uploaded = skipped = sent_bytes = 0
    failed = []
    for key in sorted(local):
        n = local[key]
        remote = f"{key_prefix}{key}"
        try:
            if await object_store.exists(remote) and await object_store.size(remote) == n:
                skipped += 1
                continue
            await object_store.put(remote, storage_root / key)
            uploaded += 1
            sent_bytes += n
        except Exception as e:  # 单个文件失败不中断整体，记进报告供重试
            logger.error("migrate_upload_failed", extra={"key": remote, "error": repr(e)})
            failed.append({"key": key, "error": repr(e)})
    return {"phase": "upload", "uploaded": uploaded, "skipped": skipped,
            "failed": failed, "bytes": sent_bytes}


async def _derive_key_columns(storage_root: Path, session_factory) -> dict:
    """推导 pre_cc_last_frame_key / pristine_last_frame_key 的初值。

    只在本地文件尚存时能扫出来，**事后永远无法补做**（Spec B §2.3）：
    本地目录是这些信息的唯一来源，填错的后果是 CC 还原链路对存量分镜失效。

    这里自行实现目录扫描，不 import 旧的 pristine_last_frame_path() ——
    Spec A 阶段 5 已把那些本地路径函数删掉了，且脚本只在切换窗口跑一次，
    自包含反而更清晰。

    只填当前为 NULL 的行，因此重跑变更数为 0。
    """
    storage_root = Path(storage_root)
    derived = {"pre_cc_last_frame_key": 0, "pristine_last_frame_key": 0}
    async with session_factory() as s:
        rows = (await s.execute(sa.text(
            "SELECT id, project_id, shot_id FROM shots"))).fetchall()
        for sid, pid, shot_id in rows:
            rel = f"projects/{pid}/shots/shot_{shot_id}"
            shot_dir = storage_root / rel
            if not shot_dir.is_dir():
                continue

            pre_cc = shot_dir / "last_frame_pre_cc.png"
            if pre_cc.is_file():
                n = (await s.execute(sa.text(
                    "UPDATE shots SET pre_cc_last_frame_key = :k WHERE id = :i "
                    "AND (pre_cc_last_frame_key IS NULL OR pre_cc_last_frame_key = '')"),
                    {"k": f"{rel}/last_frame_pre_cc.png", "i": sid})).rowcount
                derived["pre_cc_last_frame_key"] += n or 0

            # pristine：last_frame*.png 里排除固定名备份，取 mtime 最新
            cands = [p for p in shot_dir.glob("last_frame*.png")
                     if p.name != "last_frame_pre_cc.png"]
            if cands:
                newest = max(cands, key=lambda p: p.stat().st_mtime)
                n = (await s.execute(sa.text(
                    "UPDATE shots SET pristine_last_frame_key = :k WHERE id = :i "
                    "AND (pristine_last_frame_key IS NULL OR pristine_last_frame_key = '')"),
                    {"k": f"{rel}/{newest.name}", "i": sid})).rowcount
                derived["pristine_last_frame_key"] += n or 0
        await s.commit()
    return derived


async def backfill(storage_root: Path, session_factory) -> dict:
    """把 DB 里的路径值回填成 COS key，并推导两个新列的初值。

    需要停写窗口。

    历史说明：本函数曾在开头调用 ``app.db._ensure_columns`` 做幂等建列，
    因为当时 ``pre_cc_last_frame_key`` / ``pristine_last_frame_key`` 两列是
    靠应用启动时的 ALTER TABLE 创建的。迁移到 Alembic 之后，这两列由
    initial revision 建出，调用方在跑本脚本前必然已经 ``alembic upgrade head``，
    故建列步骤已删除。
    """
    changed = skipped = 0
    unrecognized = []
    async with session_factory() as s:
        conn = await s.connection()
        for spec in FIELDS:
            if spec.optional_table and not await _table_exists(conn, spec.table):
                logger.info("skip_missing_table", extra={"table": spec.table})
                continue
            rows = (await s.execute(sa.text(
                f"SELECT {spec.pk}, {spec.column} FROM {spec.table} "
                f"WHERE {spec.column} IS NOT NULL AND {spec.column} <> ''"))).fetchall()
            for pk, raw in rows:
                if spec.kind == "scalar":
                    kind = classify(raw)
                    if kind == ALREADY_KEY:
                        skipped += 1
                        continue
                    if kind == UNRECOGNIZED:
                        unrecognized.append({"table": spec.table, "column": spec.column,
                                             "pk": str(pk), "raw": raw})
                        continue
                    new = to_key(raw)
                else:
                    fn = (rewrite_json_list if spec.kind == "json_list"
                          else rewrite_json_dict_of_lists)
                    new = fn(raw)
                    if new is None:
                        skipped += 1
                        continue
                await s.execute(sa.text(
                    f"UPDATE {spec.table} SET {spec.column} = :v WHERE {spec.pk} = :k"),
                    {"v": new, "k": pk})
                changed += 1
        await s.commit()

    derived = await _derive_key_columns(storage_root, session_factory)
    return {"phase": "backfill", "changed": changed, "skipped": skipped,
            "unrecognized": unrecognized, "derived": derived}


async def verify(session_factory, key_prefix: str = "",
                 baseline: Optional[list[dict]] = None) -> dict:
    """校验 DB 中每个 key 在 COS 真实存在。

    ``baseline`` 是 --scan 产出的悬空清单：那些引用的本地文件在迁移**之前**
    就已不存在（实测 349 条里有 212 条），迁移无从修复。把它们列为「预期缺失」
    单独计数，只对基线之外的缺口判失败——否则 --verify 会永远飘红，
    也就再没人拿它当红绿灯看了。
    """
    await cos_client.warm_credentials()
    expected_missing = {b["key"] for b in (baseline or []) if b.get("key")}

    refs = await collect_db_refs(session_factory)
    checked = present = missing_expected = 0
    missing_unexpected = []
    seen = set()
    for r in refs:
        if r.key is None or r.key in seen:
            continue
        seen.add(r.key)
        checked += 1
        if await object_store.exists(f"{key_prefix}{r.key}"):
            present += 1
        elif r.key in expected_missing:
            missing_expected += 1
        else:
            missing_unexpected.append(asdict(r))
    return {"phase": "verify", "checked": checked, "present": present,
            "missing_expected": missing_expected,
            "missing_unexpected": missing_unexpected,
            "ok": not missing_unexpected}


def write_report(report: dict, report_dir: Path, name: str) -> Path:
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{name}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
