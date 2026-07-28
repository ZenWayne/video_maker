"""存量媒体迁移 CLI。

    uv run --project backend python -m app.scripts.migrate_to_cos --scan \
        --storage-root /app/storage

四个阶段可分别运行，全部幂等、可中断重跑。典型切换顺序见
docs/runbooks/2026-07-28-cos-cutover.md。
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from app.scripts.cos_migration.runner import backfill, scan, upload, verify, write_report


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="migrate_to_cos")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--scan", action="store_true", help="扫描本地与 DB，产出清单与悬空基线")
    g.add_argument("--upload", action="store_true", help="上传对象（可在线运行，不动 DB）")
    g.add_argument("--backfill", action="store_true", help="回填 DB 路径字段（需停写窗口）")
    g.add_argument("--verify", action="store_true", help="校验每个 key 在 COS 存在")
    p.add_argument("--storage-root", required=True, type=Path,
                   help="本地 storage 根目录（容器内通常是 /app/storage）")
    p.add_argument("--report-dir", type=Path, default=Path("migration_report"))
    p.add_argument("--key-prefix", default="",
                   help="仅测试用：把所有对象写到该前缀下，保持 dev bucket 干净")
    p.add_argument("--retry-failed", type=Path, default=None,
                   help="--upload 时按给定的上次报告只重试失败条目")
    return p


async def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _build_parser().parse_args(argv)

    # app/db.py 里 `AsyncSession` 就是 async_sessionmaker 实例（不是
    # sqlalchemy 的 AsyncSession 类），直接当 session_factory 用。绝不另建
    # 一个平行 engine —— 那会和应用用不同的连接池打同一个 sqlite 文件。
    from app.db import AsyncSession as session_factory
    sf = session_factory

    if args.scan:
        report = await scan(args.storage_root, sf)
    elif args.upload:
        only = None
        if args.retry_failed:
            prev = json.loads(args.retry_failed.read_text(encoding="utf-8"))
            only = [f["key"] for f in prev.get("failed", [])]
        report = await upload(args.storage_root, key_prefix=args.key_prefix, only=only)
    elif args.backfill:
        report = await backfill(args.storage_root, sf)
    else:
        baseline = None
        scan_path = args.report_dir / "scan.json"
        if scan_path.is_file():
            baseline = json.loads(scan_path.read_text(encoding="utf-8")).get("dangling")
        report = await verify(sf, key_prefix=args.key_prefix, baseline=baseline)

    path = write_report(report, args.report_dir, report["phase"])
    print(json.dumps(report, ensure_ascii=False, indent=2)[:4000])
    print(f"\n报告已写入 {path}")
    return 0 if report.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
