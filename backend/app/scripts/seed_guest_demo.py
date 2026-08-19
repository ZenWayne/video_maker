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

**默认只复制「有东西可看」的项目**：至少有一个分镜生成过视频。实测这一条正好把
Playwright 留下的测试项目全筛掉（它们停在 draft/scripting，从没跑到生成），
不需要按名字硬筛。加 --include-empty 可关闭。

**默认还会体检素材**：逐个探测每个 shot 的 video_path / last_frame_path 和参考图在
COS 里是否真的存在，有缺失的项目整个跳过。dev 库里确实躺着这种行（Playwright
测试留下的项目，COS 对象早被清了，DB 行还在），复制进演示集就是访客点开报 500。
体检要连 COS，所以调用时必须带上凭据：

    podman exec video-maker-backend-dev sh -c '
      export COS_SECRET_ID=$(cat /run/secrets/cos_secret_id) &&
      export COS_SECRET_KEY=$(cat /run/secrets/cos_secret_key) &&
      uv run --project . python -m app.scripts.seed_guest_demo --from stella --to guest --apply --replace'

加 --skip-media-check 可跳过体检（不建议，除非你确定素材都在）。
"""

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from app.db import AsyncSession, init_db
from app.models.project import Event, Project, ReferenceImage, Shot, User
from app.services import cos_client, object_store

# 复制 Shot 时逐列拷贝，但这几列不能带过去：id 是自增主键，project_id 要指向新
# 项目。其余（含 video_path / last_frame_path 等 COS key）原样复用。
_SHOT_SKIP = {"id", "project_id"}
_PROJECT_SKIP = {"id", "owner_id", "created_at", "updated_at"}


async def _missing_assets(project) -> list[str]:
    """列出该项目里「DB 有记录、COS 没对象」的素材 key。

    存在这种行不是假设：dev 库里有 Playwright 留下的项目，COS 对象早被清理，
    shots.video_path 还指着它们。把这种项目复制进演示集，访客点开就是 500 或
    裂图——而且因为副本共用源 key，重跑复制也修不好，只能不复制。
    """
    keys: list[str] = []
    for shot in project.shots:
        keys += [k for k in (shot.video_path, shot.last_frame_path) if k]
    keys += [img.storage_path for img in project.reference_images if img.storage_path]

    if not keys:
        return []

    # 并发探测，别 144 个对象串行等
    sem = asyncio.Semaphore(16)

    async def _check(key: str) -> str | None:
        async with sem:
            try:
                return None if await object_store.exists(key) else key
            except Exception:  # noqa: BLE001 — 探测失败按「缺失」处理，宁可少复制
                return key

    return [k for k in await asyncio.gather(*(_check(k) for k in keys)) if k]


def _copy_columns(src, model, skip: set) -> dict:
    return {
        c.name: getattr(src, c.name)
        for c in model.__table__.columns
        if c.name not in skip
    }


async def run(source: str, target: str, apply: bool, replace: bool,
              check_media: bool, require_video: bool) -> int:
    await init_db()
    if check_media:
        # object_store.exists 需要 COS 凭据；容器里 exec 进来时环境没有
        # entrypoint 导出的那几个变量，必须自己 warm 一次（会明确报错而不是静默跳过）。
        await cos_client.warm_credentials()

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

        skipped: list[tuple[str, str, int]] = []

        if require_video:
            # 「有东西可看」才配当演示。没有任何分镜生成过视频的项目，访客点进去
            # 是一个空壳——而且实测这一条正好把 Playwright 留下的测试项目全筛掉了
            # （它们停在 draft/scripting/script_review，从没跑到生成），不需要按
            # 名字硬筛。
            playable = [
                proj for proj in sources
                if any(shot.video_path for shot in proj.shots)
            ]
            dropped = len(sources) - len(playable)
            if dropped:
                print(f"      跳过 {dropped} 个没有任何分镜视频的项目（空壳，看不到东西）")
            sources = playable

        if check_media:
            print("      正在体检素材（COS 对象是否真的存在）……")
            healthy = []
            for proj in sources:
                missing = await _missing_assets(proj)
                if missing:
                    skipped.append((proj.id, proj.title, len(missing)))
                else:
                    healthy.append(proj)
            sources = healthy
            if skipped:
                print(f"      跳过 {len(skipped)} 个素材缺失的项目：")
                for pid, title, n in skipped[:10]:
                    print(f"        - {title[:32]:<32} 缺 {n} 个对象  ({pid})")
                if len(skipped) > 10:
                    print(f"        …… 另有 {len(skipped) - 10} 个")
            print(f"      素材完好、可用作演示的：{len(sources)} 个项目")

        if not sources:
            print("没有素材完好的项目可复制。", file=sys.stderr)
            return 1
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
    print(f"      校验：{target} 名下 {total} 个，{source} 名下仍为 {src_total} 个"
          + (f"（体检跳过 {len(skipped)} 个）" if skipped else ""))
    # 只校验「复制出来的数量对得上」和「源没被动过」。源总数不再等于复制数——
    # 体检会筛掉素材缺失的项目，两者相差的正是 skipped。
    if total != len(sources):
        print(f"校验失败：应复制 {len(sources)} 个，实际 {total} 个。", file=sys.stderr)
        return 1
    if src_total != len(sources) + len(skipped):
        print(f"校验失败：源账号项目数变成了 {src_total}，复制不该动源数据。", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="复制演示数据给访客账号")
    parser.add_argument("--from", dest="source", required=True, help="源账号（如 stella）")
    parser.add_argument("--to", dest="target", required=True, help="访客账号（如 guest）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    parser.add_argument("--replace", action="store_true", help="先清空访客名下现有项目")
    parser.add_argument(
        "--skip-media-check", action="store_true",
        help="跳过素材体检（默认会逐个探测 COS 对象，缺素材的项目不复制）",
    )
    parser.add_argument(
        "--include-empty", action="store_true",
        help="连没有任何分镜视频的项目也复制（默认只复制有东西可看的）",
    )
    args = parser.parse_args()
    return asyncio.run(run(
        args.source.strip().lower(), args.target.strip().lower(), args.apply, args.replace,
        not args.skip_media_check, not args.include_empty,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
