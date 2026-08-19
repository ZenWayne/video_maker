"""FR-8.3 存量数据归属迁移：把 owner_id 为 NULL 的项目归到指定账号名下。

**这是不可逆操作，且目标 PVC 没有任何备份机制**（`local-path` +
`reclaimPolicy=Delete`）。所以脚本按 FRD §4.1 的七步来，顺序不能颠倒：

    1. 备份数据库          ← 脚本会强制要求（除非显式 --skip-backup）
    2. 记录基线            ← 当天现取，别照抄文档里的数字
    3. 建 users 表 + 目标账号
    4. 建 projects.owner_id 列（init_db 的 _ensure_columns 已经幂等做了）
    5. 回填 owner_id
    6. 校验 owner_id IS NULL 为 0
    7. 确认无误后才打开 AUTH_ENFORCED（**不在本脚本内**）

默认是 **dry-run**：只报告要改什么，不写任何东西。真正执行要显式 --apply。

用法（容器内，遵循仓库既有 uv 约定）：

    # 先看要改什么（不写库）
    uv run --project . python -m app.scripts.migrate_owner_id --owner stella

    # 真的执行（会先备份到 /app/data/）
    uv run --project . python -m app.scripts.migrate_owner_id --owner stella --apply

目标账号必须**先建好**（`manage_users create stella --admin`）：脚本不替你造账号，
免得把项目回填给一个打错字的用户名。
"""

import argparse
import asyncio
import shutil
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select, update

from app.config import settings
from app.db import AsyncSession, init_db
from app.models.project import Project, User


def _sqlite_path() -> Path | None:
    """从 DATABASE_URL 解析出 sqlite 文件路径；非 sqlite 返回 None。"""
    url = settings.database_url
    if not url.startswith("sqlite"):
        return None
    # sqlite+aiosqlite:////app/data/dev.db → /app/data/dev.db
    _, _, tail = url.partition("///")
    return Path("/" + tail.lstrip("/")) if tail else None


def backup_database() -> Path:
    """整库文件级备份。

    用文件拷贝而不是 `.dump`：拷贝出来的就是一个可以直接换回去的库，恢复时不需要
    再跑一遍导入。备份放在同一个 PVC 上只防「脚本写错」，**不防 PVC 被删**——
    真要保命还得把它拷到集群外。
    """
    db_path = _sqlite_path()
    if db_path is None:
        raise SystemExit(
            f"DATABASE_URL 不是 sqlite（{settings.database_url}）——"
            "备份方式不同，请手工备份后加 --skip-backup 重跑。"
        )
    if not db_path.exists():
        raise SystemExit(f"数据库文件不存在：{db_path}")

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    dest = db_path.with_name(f"{db_path.stem}.pre-owner-migration.{stamp}.db")
    shutil.copy2(db_path, dest)
    print(f"[1/6] 已备份：{dest}（{dest.stat().st_size} 字节）")
    return dest


async def run(owner_username: str, apply: bool, skip_backup: bool) -> int:
    # 建表/建列走既有机制（create_all + _ensure_columns），保证 owner_id 列存在。
    await init_db()

    async with AsyncSession() as session:
        owner = (await session.execute(
            select(User).where(User.username == owner_username)
        )).scalar_one_or_none()
        if owner is None:
            print(
                f"目标账号不存在：{owner_username}\n"
                f"先建好再跑：uv run --project . python -m app.scripts.manage_users "
                f"create {owner_username} --admin",
                file=sys.stderr,
            )
            return 1

        total = (await session.execute(
            select(func.count()).select_from(Project)
        )).scalar_one()
        orphan = (await session.execute(
            select(func.count()).select_from(Project).where(Project.owner_id.is_(None))
        )).scalar_one()
        already_owned = total - orphan

        print(f"[2/6] 基线：projects={total}，其中 owner_id 为 NULL 的 {orphan} 个，"
              f"已有归属 {already_owned} 个")
        print(f"[3/6] 目标账号：{owner_username}（id={owner.id}，admin={bool(owner.is_admin)}）")

        # 展示一下将被改动的样本，避免「以为改了 A 其实改了 B」。
        sample = (await session.execute(
            select(Project.id, Project.title, Project.creator_name)
            .where(Project.owner_id.is_(None))
            .order_by(Project.created_at)
            .limit(5)
        )).all()
        for pid, title, creator in sample:
            print(f"        - {pid}  creator_name={creator!r}  {title[:24]}")
        if orphan > len(sample):
            print(f"        …… 另有 {orphan - len(sample)} 个")

        if orphan == 0:
            print("[4/6] 没有需要回填的行，跳过。")
            print("[6/6] 校验：owner_id IS NULL 计数 = 0 ✓")
            return 0

        if not apply:
            print("\n== DRY RUN ==（没有写入任何东西）")
            print(f"加 --apply 才会把这 {orphan} 个项目的 owner_id 设为 {owner_username}。")
            return 0

    if not skip_backup:
        backup_database()
    else:
        print("[1/6] 已按要求跳过备份（--skip-backup）")

    async with AsyncSession() as session:
        # 只动 owner_id IS NULL 的行。**不要**按 creator_name='anonymous' 过滤：
        # 权威字段是 owner_id，按展示字段过滤会漏掉 creator_name 被改过的存量行。
        result = await session.execute(
            update(Project).where(Project.owner_id.is_(None)).values(owner_id=owner.id)
        )
        await session.commit()
        print(f"[5/6] 已回填 {result.rowcount} 行")

    async with AsyncSession() as session:
        remaining = (await session.execute(
            select(func.count()).select_from(Project).where(Project.owner_id.is_(None))
        )).scalar_one()
        owned_now = (await session.execute(
            select(func.count()).select_from(Project).where(Project.owner_id == owner.id)
        )).scalar_one()
        total_now = (await session.execute(
            select(func.count()).select_from(Project)
        )).scalar_one()

    print(f"[6/6] 校验：owner_id IS NULL = {remaining}（应为 0），"
          f"{owner_username} 名下 {owned_now} 个，总计 {total_now} 个")

    if remaining != 0:
        print("校验失败：仍有未归属的项目，**不要**打开 AUTH_ENFORCED。", file=sys.stderr)
        return 1
    if total_now != total:
        print(f"校验失败：项目总数从 {total} 变成了 {total_now}。", file=sys.stderr)
        return 1

    print(
        "\n迁移完成。下一步（按顺序）：\n"
        f"  1. 用 {owner_username} 登录，确认 /api/projects 的 total == {total_now}\n"
        "  2. 把 MACHINE_TOKEN_USER 设为该账号并重建容器/滚动重启 Pod\n"
        "  3. 最后才把 AUTH_ENFORCED 改成 true\n"
        "  ⚠️ 改 config.env / ConfigMap 后必须重建容器，`restart` 读不到新值。"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="FR-8.3 存量项目归属回填")
    parser.add_argument("--owner", required=True, help="承接存量数据的账号名（如 stella）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只做 dry-run）")
    parser.add_argument(
        "--skip-backup", action="store_true",
        help="跳过自动备份（仅在你已经手工备份过时使用）",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.owner.strip().lower(), args.apply, args.skip_backup))


if __name__ == "__main__":
    raise SystemExit(main())
