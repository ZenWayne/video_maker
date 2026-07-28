"""迁移脚本四阶段的集成测试。

分工约定（结构性守卫 tests/unit/test_cos_gating_hygiene.py 会拦）：
只有真正要打 COS 的用例才带 cos_prefix 参数并加 @requires_cos；
scan/backfill 这种只碰本地磁盘与 DB 的用例一律不标，好让无凭证环境照跑。
"""
import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.scripts.cos_migration.runner import collect_db_refs, scan
from tests.integration.conftest_cos import requires_cos


async def _seed_legacy_rows(sf, storage_root: Path):
    """造一个「迁移前」的库：路径值都是 storage/... 相对路径。

    只写 DB + 本地文件，不碰 COS —— 这正是切换窗口第 5 步之前的真实形态。
    返回 (project_id, shot_dir)。
    """
    pid = "11111111-1111-1111-1111-111111111111"
    shot_dir = storage_root / "projects" / pid / "shots" / "shot_1"
    shot_dir.mkdir(parents=True)
    (shot_dir / "output.mp4").write_bytes(b"video-bytes")
    (shot_dir / "last_frame.png").write_bytes(b"png-bytes")
    (storage_root / "projects" / pid / "storyboard.json").write_text("{}")

    async with sf() as s:
        await s.execute(sa.text(
            "INSERT INTO projects (id,title,theme_text,creator_name,status,aspect_ratio,"
            "storyboard_path,auto_voice_calibrate) "
            "VALUES (:i,'t','t','a','draft','9:16',:sb,0)"),
            {"i": pid, "sb": f"storage/projects/{pid}/storyboard.json"})
        await s.execute(sa.text(
            "INSERT INTO shots (project_id,shot_id,text,shot_type,visual_description,shot_duration,"
            "status,align_with_previous,use_prev_last_frame,auto_trim,video_path,last_frame_path) "
            "VALUES (:p,1,'t','Wide','v',4,'completed',1,1,1,:v,:l)"),
            {"p": pid,
             "v": f"storage/projects/{pid}/shots/shot_1/output.mp4",
             "l": f"storage/projects/{pid}/shots/shot_1/last_frame.png"})
        await s.commit()
    return pid, shot_dir


async def test_collect_db_refs_classifies_legacy_relative_values(db_session_factory, tmp_path):
    pid, _ = await _seed_legacy_rows(db_session_factory, tmp_path)
    refs = await collect_db_refs(db_session_factory)
    by_col = {(r.table, r.column): r for r in refs}
    assert by_col[("shots", "video_path")].key == f"projects/{pid}/shots/shot_1/output.mp4"
    assert by_col[("projects", "storyboard_path")].key == f"projects/{pid}/storyboard.json"
    # 全部应被判为待转换，绝不能被判成「已经是 key」
    assert all(r.kind == "legacy_relative" for r in refs)


async def test_scan_reports_dangling_and_unreferenced(db_session_factory, tmp_path):
    pid, shot_dir = await _seed_legacy_rows(db_session_factory, tmp_path)
    # 制造一条悬空引用：DB 有 last_frame_path，但把文件删掉
    (shot_dir / "last_frame.png").unlink()
    # 制造一个未被引用的本地文件
    (shot_dir / "leftover.png").write_bytes(b"x")

    report = await scan(tmp_path, db_session_factory)

    assert report["db"]["legacy_relative"] == 3
    assert report["db"]["already_key"] == 0
    dangling_keys = {d["key"] for d in report["dangling"]}
    assert dangling_keys == {f"projects/{pid}/shots/shot_1/last_frame.png"}
    assert report["unreferenced_local"]["files"] == 1
    assert report["local"]["files"] == 3   # output.mp4 + storyboard.json + leftover.png
