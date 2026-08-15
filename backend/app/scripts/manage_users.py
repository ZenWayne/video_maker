"""账号管理 CLI —— 引导第一个管理员，之后发放点数走 API。

自助注册出来的账号一律 ``is_admin=False``、余额 0（FR-0），所以**第一个管理员
只能从进程外造**，否则「发放权只在管理员手里」这条会变成一个先有鸡还是先有蛋
的死锁。

用法（容器内，遵循仓库既有 uv 约定）：

    uv run --project . python -m app.scripts.manage_users create <用户名> --admin
    uv run --project . python -m app.scripts.manage_users promote <用户名>
    uv run --project . python -m app.scripts.manage_users grant <用户名> 500 --reason "定向投喂"
    uv run --project . python -m app.scripts.manage_users list

口令从 stdin 读（不走命令行参数），避免落进 shell 历史和 ps 输出。
"""

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.db import AsyncSession, init_db
from app.models.project import CreditLedger, CreditReason, User
from app.services import credits as credits_service
from app.services.auth import hash_password


async def _create(username: str, is_admin: bool, initial_credits: int) -> int:
    await init_db()
    password = getpass.getpass(f"为 {username} 设置口令: ")
    confirm = getpass.getpass("再输入一次: ")
    if password != confirm:
        print("两次输入不一致", file=sys.stderr)
        return 1
    if len(password) < 8:
        print("口令至少 8 位", file=sys.stderr)
        return 1

    async with AsyncSession() as session:
        exists = (await session.execute(
            select(User).where(User.username == username)
        )).scalar_one_or_none()
        if exists is not None:
            print(f"用户已存在：{username}", file=sys.stderr)
            return 1
        user = User(
            username=username,
            password_hash=hash_password(password),
            credits=initial_credits,
            is_admin=is_admin,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        if initial_credits:
            session.add(CreditLedger(
                user_id=user.id,
                delta=initial_credits,
                reason=CreditReason.GRANT.value,
                ref_type="bootstrap",
                ref_id="manage_users",
            ))
        await session.commit()
        print(f"已创建 {username}（admin={is_admin}, credits={initial_credits}）")
    return 0


async def _promote(username: str, demote: bool) -> int:
    await init_db()
    async with AsyncSession() as session:
        user = (await session.execute(
            select(User).where(User.username == username)
        )).scalar_one_or_none()
        if user is None:
            print(f"用户不存在：{username}", file=sys.stderr)
            return 1
        user.is_admin = not demote
        await session.commit()
        print(f"{username} is_admin={user.is_admin}")
    return 0


async def _grant(username: str, delta: int, reason: str) -> int:
    await init_db()
    user = await credits_service.grant(username, delta, reason)
    print(f"{username} 余额 = {user.credits}")
    return 0


async def _list() -> int:
    await init_db()
    async with AsyncSession() as session:
        rows = (await session.execute(select(User).order_by(User.created_at))).scalars().all()
        if not rows:
            print("(无账号)")
        for u in rows:
            print(f"{u.username:<24} credits={u.credits:<8} admin={bool(u.is_admin)} active={bool(u.is_active)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="video-maker 账号管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="创建账号")
    p_create.add_argument("username")
    p_create.add_argument("--admin", action="store_true", help="设为管理员")
    p_create.add_argument("--credits", type=int, default=0, help="初始点数（默认 0）")

    p_promote = sub.add_parser("promote", help="设/撤管理员")
    p_promote.add_argument("username")
    p_promote.add_argument("--demote", action="store_true")

    p_grant = sub.add_parser("grant", help="发放/回收点数")
    p_grant.add_argument("username")
    p_grant.add_argument("delta", type=int)
    p_grant.add_argument("--reason", default="cli")

    sub.add_parser("list", help="列出账号")

    args = parser.parse_args()
    if args.cmd == "create":
        return asyncio.run(_create(args.username.strip().lower(), args.admin, args.credits))
    if args.cmd == "promote":
        return asyncio.run(_promote(args.username.strip().lower(), args.demote))
    if args.cmd == "grant":
        return asyncio.run(_grant(args.username.strip().lower(), args.delta, args.reason))
    return asyncio.run(_list())


if __name__ == "__main__":
    raise SystemExit(main())
