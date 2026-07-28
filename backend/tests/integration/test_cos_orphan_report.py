"""孤儿巡检：报告准确性 + 绝不删除。"""
import pytest
import sqlalchemy as sa

from app.scripts.cos_orphan_report import find_orphans
from tests.integration.conftest_cos import requires_cos


@requires_cos
async def test_reports_known_orphans_and_spares_referenced_objects(
        db_session_factory, tmp_path, cos_prefix):
    from app.services import object_store

    pid = "22222222-2222-2222-2222-222222222222"
    referenced = f"projects/{pid}/shots/shot_1/output.mp4"
    orphan_a = f"projects/{pid}/shots/shot_1/leftover_a.mp4"
    orphan_b = f"analyses/{pid}/samples/1/leftover_b.mp4"

    for k in (referenced, orphan_a, orphan_b):
        p = tmp_path / "blob.bin"
        p.write_bytes(b"x" * 16)
        await object_store.put(f"{cos_prefix}{k}", p)

    async with db_session_factory() as s:
        await s.execute(sa.text(
            "INSERT INTO projects (id,title,theme_text,creator_name,status,aspect_ratio,"
            "auto_voice_calibrate) VALUES (:i,'t','t','a','draft','9:16',0)"), {"i": pid})
        await s.execute(sa.text(
            "INSERT INTO shots (project_id,shot_id,text,shot_type,visual_description,shot_duration,"
            "status,align_with_previous,use_prev_last_frame,auto_trim,video_path) "
            "VALUES (:p,1,'t','Wide','v',4,'completed',1,1,1,:v)"),
            {"p": pid, "v": referenced})
        await s.commit()

    report = await find_orphans(db_session_factory, key_prefix=cos_prefix)

    assert set(report["orphans_keys"]) == {orphan_a, orphan_b}
    assert report["count"] == 2
    assert referenced not in report["orphans_keys"]
    # analyses/ 前缀必须被覆盖——Spec B §2.2 漏掉 reference_samples 就是
    # 因为只想着 projects/
    assert any(k.startswith("analyses/") for k in report["orphans_keys"])


@requires_cos
async def test_never_deletes_anything(db_session_factory, tmp_path, cos_prefix, monkeypatch):
    """本工具是整套设计里唯一有不可逆破坏风险的组件，本次只做 dry-run。
    这里把所有删除入口换成会炸的替身——不是在 mock 被测逻辑，而是断言
    「删除从未被调用」这件事本身。"""
    from app.services import object_store

    p = tmp_path / "blob.bin"
    p.write_bytes(b"x" * 16)
    await object_store.put(f"{cos_prefix}projects/zzz/orphan.mp4", p)

    def _boom(*a, **k):
        raise AssertionError("巡检工具绝不允许删除任何对象")

    monkeypatch.setattr(object_store, "delete", _boom)
    monkeypatch.setattr(object_store, "delete_prefix", _boom)

    report = await find_orphans(db_session_factory, key_prefix=cos_prefix)
    assert report["count"] >= 1
    # 对象仍在
    assert await object_store.exists(f"{cos_prefix}projects/zzz/orphan.mp4")
