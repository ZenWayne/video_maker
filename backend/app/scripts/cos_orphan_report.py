"""孤儿对象巡检 —— **只读，绝不删除**。

Spec A 的一致性约定（宁可留孤儿，绝不留悬空引用）会持续产生无人引用的
对象。本工具比对 COS 实际对象与 DB 中全部 key 引用，输出孤儿清单与总体积。

为什么只做 dry-run（Spec B §5）：它是整套设计里唯一具备不可逆破坏风险的
组件。先用报告观察真实孤儿量与增长速度，再决定是否值得开启自动清理；
若孤儿量微不足道，自动清理就不必做。开启自动删除的前置条件：bucket 多版本
控制已开启（当前**未开启**）、dry-run 报告经过至少数周观察、删除本身也先
跑 dry-run。

    uv run --project backend python -m app.scripts.cos_orphan_report --older-than-days 7
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from app.scripts.cos_migration.runner import collect_db_refs
from app.services import cos_client

logger = logging.getLogger(__name__)

DEFAULT_PREFIXES = ("projects/", "analyses/")


def _parse_last_modified(value: str):
    """COS 返回的 ISO8601，形如 2026-07-27T09:05:11.000Z。"""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


async def find_orphans(session_factory, prefixes=DEFAULT_PREFIXES,
                       older_than_days: int = 0, key_prefix: str = "") -> dict:
    """列出 COS 上无人引用的对象。**不删除任何东西。**"""
    await cos_client.warm_credentials()

    refs = await collect_db_refs(session_factory)
    referenced = {r.key for r in refs if r.key}

    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    orphans, scanned = [], 0
    for pre in prefixes:
        client = cos_client.get_client()
        marker = ""
        while True:
            r = await asyncio.to_thread(
                client.list_objects, Bucket=cos_client.bucket(),
                Prefix=f"{key_prefix}{pre}", Marker=marker)
            for obj in r.get("Contents", []):
                scanned += 1
                full = obj["Key"]
                key = full[len(key_prefix):] if key_prefix else full
                if key in referenced:
                    continue
                if older_than_days:
                    lm = _parse_last_modified(obj.get("LastModified", ""))
                    if lm is not None and lm > cutoff:
                        continue
                orphans.append({"key": key, "bytes": int(obj["Size"]),
                                "last_modified": obj.get("LastModified")})
            if r.get("IsTruncated") != "true":
                break
            marker = r.get("NextMarker") or (r["Contents"][-1]["Key"] if r.get("Contents") else "")
            if not marker:
                break

    return {
        "phase": "orphan_report",
        "dry_run": True,
        "scanned": scanned,
        "count": len(orphans),
        "bytes": sum(o["bytes"] for o in orphans),
        "orphans_keys": [o["key"] for o in orphans],
        "orphans": sorted(orphans, key=lambda o: -o["bytes"])[:200],
    }


async def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="cos_orphan_report")
    p.add_argument("--older-than-days", type=int, default=7,
                   help="只报告早于该天数的对象，避开正在写入的新对象")
    p.add_argument("--key-prefix", default="", help="仅测试用")
    args = p.parse_args(argv)

    from app.db import AsyncSession as session_factory
    report = await find_orphans(session_factory,
                                older_than_days=args.older_than_days,
                                key_prefix=args.key_prefix)
    print(json.dumps(report, ensure_ascii=False, indent=2)[:8000])
    print(f"\n孤儿对象 {report['count']} 个，合计 {report['bytes'] / 1e6:.1f} MB")
    print("本工具为 dry-run，未删除任何对象。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
