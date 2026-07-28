"""迁移脚本四阶段的集成测试。

分工约定（结构性守卫 tests/unit/test_cos_gating_hygiene.py 会拦）：
只有真正要打 COS 的用例才带 cos_prefix 参数并加 @requires_cos；
scan/backfill 这种只碰本地磁盘与 DB 的用例一律不标，好让无凭证环境照跑。
"""
import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.scripts.cos_migration.runner import (
    backfill, collect_db_refs, scan, upload, verify,
)
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


@requires_cos
async def test_upload_is_idempotent_and_resumable(db_session_factory, tmp_path, cos_prefix):
    """Spec B §2.1 的验收标准：连跑两次，第二次变更数必须为 0。
    并且中断后重跑只补差量。"""
    from app.services import object_store

    pid, shot_dir = await _seed_legacy_rows(db_session_factory, tmp_path)

    # 模拟「上传到一半中断」：先只传其中一个文件
    first = await upload(tmp_path, key_prefix=cos_prefix,
                         only=[f"projects/{pid}/shots/shot_1/output.mp4"])
    assert first["uploaded"] == 1

    # 补跑全量：只应补上剩下的两个，已传的那个被跳过
    second = await upload(tmp_path, key_prefix=cos_prefix)
    assert second["uploaded"] == 2
    assert second["skipped"] == 1
    assert second["failed"] == []

    # 第三次：全部跳过，变更数为 0
    third = await upload(tmp_path, key_prefix=cos_prefix)
    assert third["uploaded"] == 0
    assert third["skipped"] == 3

    # 真实校验对象确实在 COS 上，且内容正确
    assert await object_store.exists(f"{cos_prefix}projects/{pid}/shots/shot_1/output.mp4")
    dest = tmp_path / "roundtrip.mp4"
    await object_store.get(f"{cos_prefix}projects/{pid}/shots/shot_1/output.mp4", dest)
    assert dest.read_bytes() == b"video-bytes"


@requires_cos
async def test_upload_reuploads_when_size_differs(db_session_factory, tmp_path, cos_prefix):
    """大小不一致说明本地文件在停服前又被改过，必须重传而不是跳过。"""
    pid, shot_dir = await _seed_legacy_rows(db_session_factory, tmp_path)
    await upload(tmp_path, key_prefix=cos_prefix)

    (shot_dir / "output.mp4").write_bytes(b"video-bytes-but-longer")
    again = await upload(tmp_path, key_prefix=cos_prefix)
    assert again["uploaded"] == 1
    assert again["skipped"] == 2


async def test_backfill_converts_scalar_and_json_fields_idempotently(db_session_factory, tmp_path):
    pid, _ = await _seed_legacy_rows(db_session_factory, tmp_path)
    # JSON 数组字段：Spec B 点名最容易遗漏的地方
    async with db_session_factory() as s:
        await s.execute(sa.text(
            "UPDATE shots SET custom_reference_paths = :v WHERE project_id = :p"),
            {"p": pid, "v": json.dumps([
                f"storage/projects/{pid}/shots/shot_1/custom_frames/a.jpg",
                f"storage/projects/{pid}/shots/shot_1/custom_frames/b.jpg"])})
        await s.commit()

    first = await backfill(tmp_path, db_session_factory)
    assert first["changed"] > 0

    async with db_session_factory() as s:
        row = (await s.execute(sa.text(
            "SELECT video_path, custom_reference_paths FROM shots WHERE project_id = :p"),
            {"p": pid})).first()
    assert row[0] == f"projects/{pid}/shots/shot_1/output.mp4"
    assert json.loads(row[1]) == [
        f"projects/{pid}/shots/shot_1/custom_frames/a.jpg",
        f"projects/{pid}/shots/shot_1/custom_frames/b.jpg"]

    # Spec B §2.1 验收标准：第二次变更数必须为 0
    second = await backfill(tmp_path, db_session_factory)
    assert second["changed"] == 0


async def test_backfill_derives_new_key_columns(db_session_factory, tmp_path):
    """两个新列的初值只能在本地文件尚存时推导，事后无法补做（Spec B §2.3）。
    pristine 取 last_frame_*.png 中排除固定名备份后 mtime 最新的那个。"""
    import os, time
    pid, shot_dir = await _seed_legacy_rows(db_session_factory, tmp_path)
    (shot_dir / "last_frame_pre_cc.png").write_bytes(b"pre-cc")
    (shot_dir / "last_frame_1700000000_aaaa.png").write_bytes(b"older")
    (shot_dir / "last_frame_1800000000_bbbb.png").write_bytes(b"newest")
    now = time.time()
    os.utime(shot_dir / "last_frame_1700000000_aaaa.png", (now - 500, now - 500))
    os.utime(shot_dir / "last_frame_1800000000_bbbb.png", (now, now))

    await backfill(tmp_path, db_session_factory)

    async with db_session_factory() as s:
        row = (await s.execute(sa.text(
            "SELECT pre_cc_last_frame_key, pristine_last_frame_key FROM shots "
            "WHERE project_id = :p"), {"p": pid})).first()
    assert row[0] == f"projects/{pid}/shots/shot_1/last_frame_pre_cc.png"
    assert row[1] == f"projects/{pid}/shots/shot_1/last_frame_1800000000_bbbb.png"


async def test_backfill_leaves_unrecognized_values_untouched(db_session_factory, tmp_path):
    """认不出形态的值绝不猜着改，只记进报告。"""
    pid, _ = await _seed_legacy_rows(db_session_factory, tmp_path)
    async with db_session_factory() as s:
        await s.execute(sa.text(
            "UPDATE projects SET final_video_path = 'weird/thing.mp4' WHERE id = :p"),
            {"p": pid})
        await s.commit()

    report = await backfill(tmp_path, db_session_factory)

    async with db_session_factory() as s:
        v = (await s.execute(sa.text(
            "SELECT final_video_path FROM projects WHERE id = :p"), {"p": pid})).scalar_one()
    assert v == "weird/thing.mp4"
    assert any(u["raw"] == "weird/thing.mp4" for u in report["unrecognized"])


@requires_cos
async def test_verify_passes_when_every_key_exists(db_session_factory, tmp_path, cos_prefix):
    pid, _ = await _seed_legacy_rows(db_session_factory, tmp_path)
    await upload(tmp_path, key_prefix=cos_prefix)
    await backfill(tmp_path, db_session_factory)

    report = await verify(db_session_factory, key_prefix=cos_prefix)
    assert report["ok"] is True
    assert report["missing_unexpected"] == []
    assert report["present"] == report["checked"]


@requires_cos
async def test_verify_tolerates_baseline_dangling_but_fails_on_new_gaps(
        db_session_factory, tmp_path, cos_prefix):
    """迁移前就已破损的引用进基线、不判失败；基线之外的缺失必须判失败，
    否则 --verify 这盏红绿灯就没有意义了。"""
    pid, shot_dir = await _seed_legacy_rows(db_session_factory, tmp_path)
    # last_frame 本地就没有 → 迁移前既有破损，进基线
    (shot_dir / "last_frame.png").unlink()

    scan_report = await scan(tmp_path, db_session_factory)
    await upload(tmp_path, key_prefix=cos_prefix)
    await backfill(tmp_path, db_session_factory)

    ok = await verify(db_session_factory, key_prefix=cos_prefix,
                      baseline=scan_report["dangling"])
    assert ok["ok"] is True
    assert ok["missing_expected"] == 1
    assert ok["missing_unexpected"] == []

    # 现在人为制造一个基线之外的缺口：删掉已上传的 output.mp4
    from app.services import object_store
    await object_store.delete(f"{cos_prefix}projects/{pid}/shots/shot_1/output.mp4")

    bad = await verify(db_session_factory, key_prefix=cos_prefix,
                       baseline=scan_report["dangling"])
    assert bad["ok"] is False
    assert [m["key"] for m in bad["missing_unexpected"]] == [
        f"projects/{pid}/shots/shot_1/output.mp4"]


async def test_cli_verify_warns_loudly_when_scan_json_missing(
        db_session_factory, tmp_path, monkeypatch, capsys):
    """终审 Important #1：--report-dir 下没有 scan.json 时，--verify 必须在
    stderr 打出显眼警告（点名它找的路径），而不是静默地拿 baseline=None 跑。
    真正的 verify() 阶段会打 COS（此环境无凭证），所以这里把 CLI 模块里
    绑定的 `verify` 换成一个记录调用参数的桩函数——测的是 main() 里「发现
    scan.json 缺失该怎么办」这段 CLI 级判断逻辑，不是 verify() 本身。"""
    import app.db as db_module
    import app.scripts.migrate_to_cos as cli_module

    monkeypatch.setattr(db_module, "AsyncSession", db_session_factory)

    calls = []

    async def fake_verify(sf, key_prefix="", baseline=None):
        calls.append(baseline)
        return {"phase": "verify", "checked": 0, "present": 0,
                "missing_expected": 0, "missing_unexpected": [], "ok": True}

    monkeypatch.setattr(cli_module, "verify", fake_verify)

    report_dir = tmp_path / "reports"  # 特意不预先写 scan.json
    code = await cli_module.main(["--verify", "--storage-root", str(tmp_path),
                                  "--report-dir", str(report_dir)])

    assert code == 0
    assert calls == [None]  # 没有基线，真的传了 baseline=None 下去

    err = capsys.readouterr().err
    assert "警告" in err
    assert str(report_dir / "scan.json") in err
    assert "--scan" in err  # 必须指路怎么修


async def test_cli_verify_no_warning_when_scan_json_present(
        db_session_factory, tmp_path, monkeypatch, capsys):
    """有基线文件时不该报警，且 baseline 要被正确读出并传下去。"""
    import app.db as db_module
    import app.scripts.migrate_to_cos as cli_module

    monkeypatch.setattr(db_module, "AsyncSession", db_session_factory)

    report_dir = tmp_path / "reports"
    report_dir.mkdir(parents=True)
    dangling = [{"table": "shots", "column": "video_path", "pk": "1",
                "raw": "x", "key": "projects/p/shots/shot_1/output.mp4", "kind": "legacy_relative"}]
    (report_dir / "scan.json").write_text(json.dumps({"phase": "scan", "dangling": dangling}))

    calls = []

    async def fake_verify(sf, key_prefix="", baseline=None):
        calls.append(baseline)
        return {"phase": "verify", "checked": 0, "present": 0,
                "missing_expected": 0, "missing_unexpected": [], "ok": True}

    monkeypatch.setattr(cli_module, "verify", fake_verify)

    code = await cli_module.main(["--verify", "--storage-root", str(tmp_path),
                                  "--report-dir", str(report_dir)])

    assert code == 0
    assert calls == [dangling]
    assert "警告" not in capsys.readouterr().err


async def test_cli_upload_exits_nonzero_when_failed_list_nonempty(
        db_session_factory, tmp_path, monkeypatch, capsys):
    """终审 Important #2：--upload 报告里 failed 非空时，CLI 必须退出非 0。
    真正的 upload() 一上来就 warm_credentials()（此环境无凭证），所以同样
    在 CLI 模块层面换成桩函数——测的是「main() 怎么解读 upload() 返回的
    dict」这段纯 CLI 逻辑，不重新验证 upload() 本身（那是
    test_upload_is_idempotent_and_resumable 等 @requires_cos 用例的职责）。"""
    import app.db as db_module
    import app.scripts.migrate_to_cos as cli_module

    monkeypatch.setattr(db_module, "AsyncSession", db_session_factory)

    async def fake_upload(storage_root, key_prefix="", only=None):
        return {"phase": "upload", "uploaded": 3, "skipped": 1,
                "failed": [{"key": "projects/p/shots/shot_1/output.mp4", "error": "boom"}],
                "bytes": 123}

    monkeypatch.setattr(cli_module, "upload", fake_upload)

    report_dir = tmp_path / "reports"
    code = await cli_module.main(["--upload", "--storage-root", str(tmp_path),
                                  "--report-dir", str(report_dir)])

    assert code == 1
    err = capsys.readouterr().err
    assert "1 个对象上传失败" in err
    assert str(report_dir / "upload.json") in err
    written = json.loads((report_dir / "upload.json").read_text())
    assert written["failed"]


async def test_cli_upload_exits_zero_when_nothing_failed(
        db_session_factory, tmp_path, monkeypatch):
    """对照组：failed 为空时仍应退出 0，不能矫枉过正。"""
    import app.db as db_module
    import app.scripts.migrate_to_cos as cli_module

    monkeypatch.setattr(db_module, "AsyncSession", db_session_factory)

    async def fake_upload(storage_root, key_prefix="", only=None):
        return {"phase": "upload", "uploaded": 3, "skipped": 1, "failed": [], "bytes": 123}

    monkeypatch.setattr(cli_module, "upload", fake_upload)

    code = await cli_module.main(["--upload", "--storage-root", str(tmp_path),
                                  "--report-dir", str(tmp_path / "reports")])
    assert code == 0


async def test_cli_backfill_exits_nonzero_when_unrecognized_nonempty(
        db_session_factory, tmp_path, monkeypatch, capsys):
    """终审 Important #2 的另一半：--backfill 不碰 COS，这里跑真正的
    backfill()（不需要桩），验证 unrecognized 非空时 CLI 退出非 0。"""
    import app.db as db_module
    from app.scripts.migrate_to_cos import main

    monkeypatch.setattr(db_module, "AsyncSession", db_session_factory)

    pid, _ = await _seed_legacy_rows(db_session_factory, tmp_path)
    async with db_session_factory() as s:
        await s.execute(sa.text(
            "UPDATE projects SET final_video_path = 'weird/thing.mp4' WHERE id = :p"),
            {"p": pid})
        await s.commit()

    report_dir = tmp_path / "reports"
    code = await main(["--backfill", "--storage-root", str(tmp_path),
                       "--report-dir", str(report_dir)])

    assert code == 1
    err = capsys.readouterr().err
    assert "1 条值无法识别" in err
    written = json.loads((report_dir / "backfill.json").read_text())
    assert written["unrecognized"]


async def test_cli_scan_writes_report_file(db_session_factory, tmp_path, monkeypatch):
    """CLI 必须把报告落盘——切换手册要人工核对它。

    ``main()`` 内部用 ``app.db.AsyncSession`` 当 session_factory（绝不新建
    engine，见 runner.py 与 CLAUDE.md 的强制约定），所以这里必须把它
    monkeypatch 成本测试自己的 db_session_factory，否则会打到真实共享库。
    """
    import app.db as db_module
    from app.scripts.migrate_to_cos import main

    monkeypatch.setattr(db_module, "AsyncSession", db_session_factory)

    pid, _ = await _seed_legacy_rows(db_session_factory, tmp_path)
    report_dir = tmp_path / "reports"
    code = await main(["--scan", "--storage-root", str(tmp_path),
                       "--report-dir", str(report_dir)])
    assert code == 0
    written = json.loads((report_dir / "scan.json").read_text())
    assert written["phase"] == "scan"
    assert written["db"]["legacy_relative"] == 3
