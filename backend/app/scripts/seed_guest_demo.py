"""把某个账号名下的项目复制一份给访客账号，作为未登录时的演示数据。

访客是一个**真实账号**（见 settings.guest_username），所以演示数据就是「它名下
的项目」——不需要任何特例分支，owner 过滤天然只让访客看见这些。

复制而不是直接划归：源账号的项目原样保留，两边互不影响。访客是只读的，所以
副本也不会被改坏。

⚠️ **副本共用源项目的 COS 对象**。视频/图片动辄几百 MB，复制一份存储既慢又贵，
所以副本里的 key 仍指向 ``projects/<源项目 id>/``。后果是：删掉源项目会连带把
演示数据的素材删掉（delete_project 会清整个前缀），演示页面随之变成裂图。要么
别删源项目，要么删之前重新跑一次本脚本换个源。

用法（容器内）：

    # 看会复制什么（不写库）
    uv run --project . python -m app.scripts.seed_guest_demo --from stella --to guest

    # 真的复制
    uv run --project . python -m app.scripts.seed_guest_demo --from stella --to guest --apply

    # 重刷：先清空访客名下的项目再复制
    uv run --project . python -m app.scripts.seed_guest_demo --from stella --to guest --apply --replace
"""

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from app.db import AsyncSession, init_db
from app.models.project import Event, Project, ReferenceImage, Shot, User

# 复制 Shot 时逐列拷贝，但这几列不能带过去：id 是自增主键，project_id 要指向新
# 项目。其余（含 video_path / last_frame_path 等 COS key）原样复用。
_SHOT_SKIP = {"id", "project_id"}
_PROJECT_SKIP = {"id", "owner_id", "created_at", "updated_at"}


def _copy_columns(src, model, skip: set) -> dict:
    return {
        c.name: getattr(src, c.name)
        for c in model.__table__.columns
        if c.name not in skip
    }


async def run(source: str, target: str, apply: bool, replace: bool) -> int:
    await init_db()

    async with AsyncSession() as session:
        src_user = (await session.execute(
            select(User).where(User.username == source)
        )).scalar_one_or_none()
        dst_user = (await session.execute(
            select(User).where(User.username == target)
        )).scalar_one_or_none()

        if src_user is None:
            print(f"源账号不存在：{source}", file=sys.stderr)
            return 1
        if dst_user is None:
            print(
                f"访客账号不存在：{target}\n"
                f"先建好：uv run --project . python -m app.scripts.manage_users create {target}",
                file=sys.stderr,
            )
            return 1
        if dst_user.credits != 0:
            # 访客账号有余额就等于给所有未登录访问者开了计费权限。
            print(
                f"⚠️ 访客账号 {target} 余额为 {dst_user.credits}，不是 0。"
                "访客账号必须保持 0 点，否则任何人都能用它触发计费操作。",
                file=sys.stderr,
            )
            return 1

        sources = (await session.execute(
            select(Project)
            .where(Project.owner_id == src_user.id)
            .options(selectinload(Project.shots), selectinload(Project.reference_images))
            .order_by(Project.created_at)
        )).scalars().all()

        existing = (await session.execute(
            select(func.count()).select_from(Project).where(Project.owner_id == dst_user.id)
        )).scalar_one()

        print(f"[1/3] 源 {source}：{len(sources)} 个项目"
              f"（共 {sum(len(p.shots) for p in sources)} 个分镜）")
        print(f"[2/3] 访客 {target} 当前名下：{existing} 个项目")

        if existing and not replace:
            print(
                f"访客名下已有 {existing} 个项目。加 --replace 会先清空它们再复制；"
                "不加则拒绝执行，免得复制两份。",
                file=sys.stderr,
            )
            return 1

        if not apply:
            print("\n== DRY RUN ==（没有写入任何东西）")
            print(f"加 --apply 会复制 {len(sources)} 个项目给 {target}"
                  + ("，并先清空它名下现有的项目。" if replace and existing else "。"))
            print("注意：副本共用源项目的 COS 对象，删源项目会连带删掉演示素材。")
            return 0

        if replace and existing:
            old_ids = (await session.execute(
                select(Project.id).where(Project.owner_id == dst_user.id)
            )).scalars().all()
            # 只删 DB 行，**不碰 COS**：那些对象是源项目的，删了会把源项目搞坏。
            await session.execute(delete(Shot).where(Shot.project_id.in_(old_ids)))
            await session.execute(delete(ReferenceImage).where(ReferenceImage.project_id.in_(old_ids)))
            await session.execute(delete(Event).where(Event.project_id.in_(old_ids)))
            await session.execute(delete(Project).where(Project.id.in_(old_ids)))
            await session.commit()
            print(f"[3/3] 已清空访客名下 {len(old_ids)} 个旧副本（只删 DB 行，未动 COS）")

        copied_shots = 0
        for src in sources:
            new_id = str(uuid.uuid4())
            session.add(Project(
                id=new_id,
                owner_id=dst_user.id,
                **_copy_columns(src, Project, _PROJECT_SKIP),
            ))
            for shot in src.shots:
                session.add(Shot(project_id=new_id, **_copy_columns(shot, Shot, _SHOT_SKIP)))
                copied_shots += 1
            for img in src.reference_images:
                session.add(ReferenceImage(
                    id=str(uuid.uuid4()),
                    project_id=new_id,
                    **_copy_columns(img, ReferenceImage, {"id", "project_id"}),
                ))
        await session.commit()

    async with AsyncSession() as session:
        total = (await session.execute(
            select(func.count()).select_from(Project).where(Project.owner_id == dst_user.id)
        )).scalar_one()
        src_total = (await session.execute(
            select(func.count()).select_from(Project).where(Project.owner_id == src_user.id)
        )).scalar_one()

    print(f"[3/3] 已复制 {len(sources)} 个项目 / {copied_shots} 个分镜给 {target}")
    print(f"      校验：{target} 名下 {total} 个，{source} 名下仍为 {src_total} 个")
    if total != len(sources) or src_total != len(sources):
        print("校验失败：数量对不上。", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="复制演示数据给访客账号")
    parser.add_argument("--from", dest="source", required=True, help="源账号（如 stella）")
    parser.add_argument("--to", dest="target", required=True, help="访客账号（如 guest）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    parser.add_argument("--replace", action="store_true", help="先清空访客名下现有项目")
    args = parser.parse_args()
    return asyncio.run(run(
        args.source.strip().lower(), args.target.strip().lower(), args.apply, args.replace,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
