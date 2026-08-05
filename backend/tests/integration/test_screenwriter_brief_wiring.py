import json
import pytest
from sqlalchemy import select
from app.models.project import Project, ProjectStatus


class _FakeRedis:
    async def publish(self, *a, **k): return 0


async def test_worker_run_screenwriter_passes_attached_brief(db_session_factory, monkeypatch):
    """worker 的 run_screenwriter 任务应把 project.attached_brief_json 解析后
    作为 creation_brief 传给 agent。"""
    from worker import tasks as tasks_module

    captured = {}

    async def fake_agent(theme_text, reference_images, llm_provider,
                         aspect_ratio="16:9", creation_brief=None):
        captured["brief"] = creation_brief
        return {"storyboard": {"scene_overview": "o", "shots": []},
                "word_count_warnings": []}

    monkeypatch.setattr(tasks_module, "run_screenwriter_agent", fake_agent)

    async def _noop_publish(*a, **k): return True
    monkeypatch.setattr(tasks_module, "publish_event", _noop_publish, raising=False)

    async with db_session_factory() as s:
        p = Project(title="t", theme_text="主题X", creator_name="u",
                    status=ProjectStatus.SCRIPTING.value, aspect_ratio="9:16",
                    attached_brief_json=json.dumps({"screenwriter_directives": "开场抛悬念"}))
        s.add(p); await s.commit(); pid = p.id

    ctx = {"session_factory": db_session_factory, "redis": _FakeRedis()}
    await tasks_module.run_screenwriter(ctx, pid, "user:test")

    assert captured["brief"] == {"screenwriter_directives": "开场抛悬念"}


async def test_worker_run_screenwriter_malformed_brief_json_fails_gracefully(
    db_session_factory, monkeypatch
):
    """attached_brief_json 是损坏的 JSON 时，任务不应崩溃，项目应进入 FAILED
    并带上非空 error_message。"""
    from worker import tasks as tasks_module

    async def fake_agent(theme_text, reference_images, llm_provider,
                         aspect_ratio="16:9", creation_brief=None):
        raise AssertionError("agent 不应被调用——JSON 解析应在此之前失败")

    monkeypatch.setattr(tasks_module, "run_screenwriter_agent", fake_agent)

    async def _noop_publish(*a, **k): return True
    monkeypatch.setattr(tasks_module, "publish_event", _noop_publish, raising=False)

    async with db_session_factory() as s:
        p = Project(title="t", theme_text="主题X", creator_name="u",
                    status=ProjectStatus.SCRIPTING.value, aspect_ratio="9:16",
                    attached_brief_json="{not json")
        s.add(p); await s.commit(); pid = p.id

    ctx = {"session_factory": db_session_factory, "redis": _FakeRedis()}
    # 不应抛出异常
    await tasks_module.run_screenwriter(ctx, pid, "user:test")

    async with db_session_factory() as s:
        result = await s.execute(select(Project).where(Project.id == pid))
        project = result.scalar_one()
        assert project.status == ProjectStatus.FAILED.value
        assert project.error_message
