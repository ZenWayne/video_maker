"""内容分析 worker (run_content_analysis) 集成测试。

COS 是唯一存储：每个样本的转写在自己的一次性 workspace() 里
fetch→抽音频→ASR，退出即清，没有需要断言"已删除"的持久化 wav key
（对照旧的 local storage_root 模型，那里靠 os.remove(sample_dir/.../audio.wav)
手工收尾；新模型这段收尾代码已被整段删除，workspace() 的
`finally: shutil.rmtree(..., ignore_errors=True)` 替代了它）。

按项目 gating 约定（conftest_cos.py + test_cos_gating_hygiene.py）：只有真正
需要真实 COS 对象的测试函数才带 `cos_prefix` 参数 + `@requires_cos`；
`test_analysis_and_samples_persist` 和 `test_run_content_analysis_completed_is_idempotent`
是纯 DB 逻辑（后者在触达 workspace/COS 之前就因 COMPLETED 短路返回），
不需要 COS，因此不装饰、不使用模块级 pytestmark。

按 CLAUDE.md「必须 mock LLM/模型调用」：只 mock ``asr.transcribe``（本地
ASR 模型，慢且与本测试无关）和 ``run_content_analysis_brief``（计费的
Gemini 调用）。``extract_audio_wav`` 是纯 ffmpeg，不计费，一律真实执行。
"""
import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.exc import PendingRollbackError

from app.models.project import (
    ContentAnalysis, ReferenceSample,
    ContentAnalysisStatus, ReferenceSampleStatus,
)
from app.agents.asr import TranscriptResult
from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import seed_analysis_sample_video
from worker import tasks as tasks_module


class _FakeRedis:
    async def publish(self, *a, **k): return 0


class _CapturingRedis:
    """Records every published message (decoded) for assertions on event shape."""
    def __init__(self):
        self.published = []

    async def publish(self, channel, message):
        self.published.append(json.loads(message))
        return 1


async def test_analysis_and_samples_persist(db_session_factory):
    async with db_session_factory() as s:
        a = ContentAnalysis(title="美妆赛道-账号A", region_hint="en")
        a.samples.append(ReferenceSample(order_index=0, video_path="analyses/a1/sample_0/x.mp4"))
        a.samples.append(ReferenceSample(order_index=1, video_path="analyses/a1/sample_1/x.mp4"))
        s.add(a)
        await s.commit()
        aid = a.id

    async with db_session_factory() as s:
        row = (await s.execute(
            select(ContentAnalysis).where(ContentAnalysis.id == aid)
        )).scalar_one()
        assert row.status == ContentAnalysisStatus.UPLOADING.value
        assert len(row.samples) == 2
        assert row.samples[0].status == ReferenceSampleStatus.PENDING.value


async def _seed_analysis(db_session_factory, n_speech=3, n_silent=1):
    """Create a ContentAnalysis + N placeholder samples (video_path not yet a
    real COS key). Caller must seed real videos via seed_analysis_sample_video
    for any sample whose transcription path actually runs."""
    async with db_session_factory() as s:
        a = ContentAnalysis(title="t", region_hint="en")
        for i in range(n_speech + n_silent):
            a.samples.append(ReferenceSample(order_index=i, video_path=""))
        s.add(a)
        await s.commit()
        return a.id, [smp.id for smp in a.samples], n_speech


async def _seed_analysis_with_real_videos(db_session_factory, n_speech=3, n_silent=1):
    aid, sample_ids, n_speech = await _seed_analysis(db_session_factory, n_speech, n_silent)
    for sid in sample_ids:
        await seed_analysis_sample_video(db_session_factory, aid, sid)
    return aid, sample_ids, n_speech


@requires_cos
async def test_run_content_analysis_happy_path(db_session_factory, monkeypatch, cos_prefix):
    aid, sample_ids, n_speech = await _seed_analysis_with_real_videos(db_session_factory, 3, 1)

    # stub ASR：前 n_speech 条有语音，最后一条无语音。extract_audio_wav is NOT
    # stubbed — it runs for real via ffmpeg against the seeded video.
    calls = {"n": 0}
    def fake_transcribe(path, language=None):
        i = calls["n"]; calls["n"] += 1
        if i < n_speech:
            return TranscriptResult(True, f"transcript {i}", f"hook {i}", "en")
        return TranscriptResult(False, "", "", "en")
    monkeypatch.setattr(tasks_module.asr, "transcribe", fake_transcribe)

    # stub 计费边界：brief LLM
    async def fake_brief(samples, provider, model):
        return {
            "niche_summary": "x",
            "sample_stats": {"sample_n": 999, "no_speech_pct": 9.9, "sample_warning": "STALE"},
            "hook_strategy": {"common_hook_types": ["悬念"], "example_hooks": ["hook 0"]},
            "script_structure": {"pacing": "快", "emotion": "正向", "info_gap": "有", "cta": "评论"},
            "do": ["a"], "dont": ["b"], "screenwriter_directives": "开场抛悬念",
        }
    monkeypatch.setattr(tasks_module, "run_content_analysis_brief", fake_brief)
    # 避免真建 Vertex provider
    monkeypatch.setattr(tasks_module, "GeminiProvider", lambda **k: object())

    ctx = {"session_factory": db_session_factory, "redis": _FakeRedis()}
    await tasks_module.run_content_analysis(ctx, aid, "user:test")

    async with db_session_factory() as s:
        row = (await s.execute(
            select(ContentAnalysis).where(ContentAnalysis.id == aid))).scalar_one()
        assert row.status == ContentAnalysisStatus.COMPLETED.value
        brief = json.loads(row.brief_json)
        # 代码计算的 stats 覆盖 LLM 返回的占位
        assert brief["sample_stats"]["sample_n"] == 3
        assert brief["sample_stats"]["no_speech_pct"] == 0.25
        assert brief["sample_stats"]["sample_warning"] is None
        # 无人声样本被标记、不参与
        silent = [smp for smp in row.samples if smp.has_speech is False]
        assert len(silent) == 1
        # video_path 仍是 COS key（转写流程从不改写它）
        for smp in row.samples:
            assert smp.video_path.startswith("analyses/")


@requires_cos
async def test_run_content_analysis_all_silent_fails(db_session_factory, monkeypatch, cos_prefix):
    aid, _, _ = await _seed_analysis_with_real_videos(db_session_factory, 0, 2)
    monkeypatch.setattr(tasks_module.asr, "transcribe",
                        lambda p, language=None: TranscriptResult(False, "", "", "en"))
    ctx = {"session_factory": db_session_factory, "redis": _FakeRedis()}
    await tasks_module.run_content_analysis(ctx, aid, "user:test")
    async with db_session_factory() as s:
        row = (await s.execute(
            select(ContentAnalysis).where(ContentAnalysis.id == aid))).scalar_one()
        assert row.status == ContentAnalysisStatus.FAILED.value
        assert row.error_message


def _make_flaky_commit_session_factory(real_factory, fail_at_call: int):
    """Wraps the real session factory so the Nth commit() raises, and any
    commit() attempted afterward WITHOUT an intervening rollback() raises
    PendingRollbackError — exactly SQLAlchemy's real behaviour when a flush/
    commit fails and the session isn't rolled back before reuse. This lets us
    reproduce BLOCKING 3's regression (outer safety net committing again on a
    broken session) without needing a real DB-level constraint violation.
    """
    class _Wrapper:
        def __init__(self):
            self._real = None
            self._calls = 0
            self._poisoned = False

        async def __aenter__(self):
            self._real = real_factory()
            await self._real.__aenter__()
            return self

        async def __aexit__(self, *exc):
            return await self._real.__aexit__(*exc)

        def __getattr__(self, name):
            return getattr(self._real, name)

        async def commit(self):
            self._calls += 1
            if self._poisoned:
                raise PendingRollbackError(
                    "This Session's transaction has been rolled back due to a "
                    "previous exception during flush."
                )
            if self._calls == fail_at_call:
                self._poisoned = True
                raise RuntimeError("simulated commit failure")
            return await self._real.commit()

        async def rollback(self):
            self._poisoned = False
            return await self._real.rollback()

    return lambda: _Wrapper()


@requires_cos
async def test_run_content_analysis_commit_failure_marks_failed_without_raising(
    db_session_factory, monkeypatch, cos_prefix,
):
    """Regression for BLOCKING 3: the outer safety-net except handler must
    rollback() before attempting the failure commit. The dominant route into
    that handler is a FAILED commit; committing again on a broken session
    without rollback() raises PendingRollbackError, which escapes the task
    and leaves the analysis stuck at transcribing/analyzing forever (no
    retry/delete endpoint — the only recovery would be hand-editing the DB).
    """
    aid, sample_ids, n_speech = await _seed_analysis_with_real_videos(db_session_factory, 1, 0)

    monkeypatch.setattr(tasks_module.asr, "transcribe",
                        lambda p, language=None: TranscriptResult(True, "t", "h", "en"))

    # Commit sequence for a single sample: (1) analysis->TRANSCRIBING,
    # (2) sample->TRANSCRIBING, (3) sample->TRANSCRIBED (post-transcribe,
    # OUTSIDE the per-sample try/except). Fail call 3 — the exact "failed
    # commit escapes to the outer safety net" scenario this test guards.
    flaky_factory = _make_flaky_commit_session_factory(db_session_factory, fail_at_call=3)

    ctx = {"session_factory": flaky_factory, "redis": _FakeRedis()}
    # Must NOT raise — the fix's rollback() must let the failure-commit succeed.
    await tasks_module.run_content_analysis(ctx, aid, "user:test")

    async with db_session_factory() as s:
        row = (await s.execute(
            select(ContentAnalysis).where(ContentAnalysis.id == aid))).scalar_one()
        assert row.status == ContentAnalysisStatus.FAILED.value
        assert row.error_message


@requires_cos
async def test_run_content_analysis_cancelled_marks_failed_and_reraises(
    db_session_factory, monkeypatch, cos_prefix,
):
    """Regression for BLOCKING 3: arq enforces job_timeout by cancelling the
    task's coroutine (asyncio.CancelledError — a BaseException, NOT caught by
    `except Exception`). The very first job legitimately downloads a ~3GB ASR
    model before transcribing, so hitting the timeout is realistic. Cancellation
    must still drive the analysis to a terminal status before propagating —
    never swallowed (arq needs to observe it), never left stuck.
    """
    aid, sample_ids, n_speech = await _seed_analysis_with_real_videos(db_session_factory, 1, 0)

    monkeypatch.setattr(tasks_module.asr, "transcribe",
                        lambda p, language=None: TranscriptResult(True, "t", "h", "en"))

    # Simulate cancellation at a real await point inside the pipeline (the
    # per-sample progress publish), not by raising inside a to_thread call —
    # this is the same class of await point where arq's real job_timeout
    # cancellation would land.
    calls = {"n": 0}
    async def flaky_publish_event(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise asyncio.CancelledError()
        return True
    monkeypatch.setattr(tasks_module, "publish_event", flaky_publish_event)

    ctx = {"session_factory": db_session_factory, "redis": _FakeRedis()}
    with pytest.raises(asyncio.CancelledError):
        await tasks_module.run_content_analysis(ctx, aid, "user:test")

    async with db_session_factory() as s:
        row = (await s.execute(
            select(ContentAnalysis).where(ContentAnalysis.id == aid))).scalar_one()
        assert row.status == ContentAnalysisStatus.FAILED.value
        assert row.error_message


async def test_run_content_analysis_completed_is_idempotent(db_session_factory, monkeypatch):
    """C4: guard the billed call against re-entry. If the worker restarts
    mid-job (routine in dev), arq re-queues and would otherwise re-run the
    whole pipeline — including a SECOND billed Gemini call — for an analysis
    that already finished.

    No real COS object needed: the COMPLETED-status guard returns before the
    per-sample workspace().fetch() is ever reached — asserted below by making
    both the extraction and the brief call blow up if reached.
    """
    aid, sample_ids, n_speech = await _seed_analysis(db_session_factory, 1, 0)
    async with db_session_factory() as s:
        row = (await s.execute(
            select(ContentAnalysis).where(ContentAnalysis.id == aid))).scalar_one()
        row.status = ContentAnalysisStatus.COMPLETED.value
        row.brief_json = '{"screenwriter_directives":"already done"}'
        await s.commit()

    def boom(*a, **k):
        raise AssertionError("must not re-run transcription for an already-completed analysis")
    monkeypatch.setattr(tasks_module.asr, "transcribe", boom)
    async def boom_brief(*a, **k):
        raise AssertionError("must not re-run the billed brief call for a completed analysis")
    monkeypatch.setattr(tasks_module, "run_content_analysis_brief", boom_brief)

    ctx = {"session_factory": db_session_factory, "redis": _FakeRedis()}
    await tasks_module.run_content_analysis(ctx, aid, "user:test")

    async with db_session_factory() as s:
        row = (await s.execute(
            select(ContentAnalysis).where(ContentAnalysis.id == aid))).scalar_one()
        assert row.status == ContentAnalysisStatus.COMPLETED.value
        assert row.brief_json == '{"screenwriter_directives":"already done"}'


@requires_cos
async def test_run_content_analysis_publishes_per_sample_progress(
    db_session_factory, monkeypatch, cos_prefix,
):
    """C2: every publish used to be byte-identical ({"type": "analysis_progress",
    "status": <analysis-level status>, "analysis_id": ...}), emitted before AND
    after every per-sample transition, so a consumer could not tell which
    sample moved. Per-sample events must now carry sample identity/status."""
    aid, sample_ids, n_speech = await _seed_analysis_with_real_videos(db_session_factory, 2, 0)

    monkeypatch.setattr(tasks_module.asr, "transcribe",
                        lambda p, language=None: TranscriptResult(True, "t", "h", "en"))

    async def fake_brief(samples, provider, model):
        return {
            "niche_summary": "x", "sample_stats": {},
            "hook_strategy": {"common_hook_types": [], "example_hooks": []},
            "script_structure": {"pacing": "x", "emotion": "x", "info_gap": "x", "cta": "x"},
            "do": [], "dont": [], "screenwriter_directives": "x",
        }
    monkeypatch.setattr(tasks_module, "run_content_analysis_brief", fake_brief)
    monkeypatch.setattr(tasks_module, "GeminiProvider", lambda **k: object())

    redis = _CapturingRedis()
    ctx = {"session_factory": db_session_factory, "redis": redis}
    await tasks_module.run_content_analysis(ctx, aid, "user:test")

    per_sample_events = [
        e for e in redis.published
        if e["type"] == "analysis_progress" and "sample_id" in e["data"]
    ]
    assert per_sample_events, "no per-sample progress events were published"
    seen_sample_ids = {e["data"]["sample_id"] for e in per_sample_events}
    # Both seeded samples' progress must be individually observable.
    assert seen_sample_ids == set(sample_ids)
    for e in per_sample_events:
        assert e["data"]["sample_status"] in (
            ReferenceSampleStatus.TRANSCRIBING.value, ReferenceSampleStatus.TRANSCRIBED.value,
        )
        assert e["data"]["analysis_id"] == aid
