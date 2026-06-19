"""Tests for Langfuse session/trace chaining — one project, one trace.

These verify the grouping logic without hitting the network, using a fake
Langfuse client that mimics v4 ``trace_id`` propagation: a child observation
inherits the current trace, and ``trace_context={"trace_id": ...}`` anchors a
root span to a specific trace.
"""

import contextlib
import datetime
import hashlib

import pytest

from app import observability


class _FakeObs:
    def __init__(self, recorder, trace_id):
        self._rec = recorder
        self.trace_id = trace_id

    def __enter__(self):
        self._rec._stack.append(self.trace_id)
        return self

    def __exit__(self, *exc):
        self._rec._stack.pop()
        return False

    def update(self, **kwargs):
        self._rec.updates.append(kwargs)


class FakeLangfuse:
    """Minimal stand-in mimicking Langfuse v4 trace-id propagation."""

    def __init__(self):
        self._stack = []
        self.starts = []
        self.updates = []
        self.flushed = 0

    def create_trace_id(self, seed):
        return hashlib.sha256(str(seed).encode()).hexdigest()[:32]

    def start_as_current_observation(self, *, name, as_type, trace_context=None, **kwargs):
        if trace_context and trace_context.get("trace_id"):
            trace_id = trace_context["trace_id"]          # anchored root
        elif self._stack:
            trace_id = self._stack[-1]                     # inherit parent
        else:
            trace_id = "auto:" + name                      # orphan root
        self.starts.append({"name": name, "as_type": as_type, "trace_id": trace_id})
        return _FakeObs(self, trace_id)

    def get_current_trace_id(self):
        return self._stack[-1] if self._stack else None

    def flush(self):
        self.flushed += 1


@pytest.fixture
def fake_lf(monkeypatch):
    fake = FakeLangfuse()
    monkeypatch.setattr(observability, "_client", fake)
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "get_langfuse", lambda: fake)
    # session() uses the real Langfuse propagate_attributes; stub it to a
    # passthrough so these tests stay focused on trace-id grouping.
    @contextlib.contextmanager
    def _noop_session(session_id, tags=None, metadata=None):
        yield session_id

    monkeypatch.setattr(observability, "session", _noop_session)
    return fake


def test_make_session_id_deterministic_with_timestamp():
    dt = datetime.datetime(2026, 6, 17, 6, 29, 1)
    a = observability.make_session_id("p1", dt)
    assert a == observability.make_session_id("p1", dt)        # stable per project
    assert a != observability.make_session_id("p2", dt)        # differs by project
    assert a == f"vm-{hashlib.sha256(b'p1').hexdigest()[:12]}-{int(dt.timestamp())}"


def test_make_session_id_hash_only_fallback():
    assert observability.make_session_id("p1") == f"vm-{hashlib.sha256(b'p1').hexdigest()[:12]}"


def test_make_trace_id_deterministic(fake_lf):
    a = observability.make_trace_id("proj-x")
    assert a == observability.make_trace_id("proj-x")          # stable per project
    assert a != observability.make_trace_id("proj-y")
    assert len(a) == 32 and all(c in "0123456789abcdef" for c in a)


def test_two_jobs_for_project_chain_into_one_trace(fake_lf):
    """Two separate worker jobs + nested generations land in ONE trace."""
    trace_id = observability.make_trace_id("projAAA")

    with observability.trace("worker-screenwriter-run", trace_id=trace_id):
        with observability.generation("agents-screenwriter-generate-storyboard", model="m"):
            pass
    with observability.trace("worker-shot-pipeline-run", trace_id=trace_id):
        with observability.generation("agents-director-generate-motion", model="m"):
            pass

    assert len(fake_lf.starts) == 4                            # 2 roots + 2 generations
    assert {s["trace_id"] for s in fake_lf.starts} == {trace_id}


def test_different_projects_get_different_traces(fake_lf):
    with observability.trace("worker-screenwriter-run", trace_id=observability.make_trace_id("A")):
        pass
    with observability.trace("worker-screenwriter-run", trace_id=observability.make_trace_id("B")):
        pass
    assert len({s["trace_id"] for s in fake_lf.starts}) == 2


async def test_traced_job_groups_all_jobs_by_project(fake_lf, monkeypatch):
    """The decorator anchors every job for a project to its single trace id."""
    async def fake_resolve(project_id):
        return "vm-session"

    monkeypatch.setattr(observability, "resolve_session_for", fake_resolve)

    @observability.traced_job("worker-test-run", tags=["t"])
    async def job(ctx, project_id, extra):
        with observability.generation("agents-x-run", model="m"):
            pass
        return f"done:{extra}"

    r1 = await job({"redis": None}, "projZ", "a")
    r2 = await job({"redis": None}, "projZ", "b")

    assert (r1, r2) == ("done:a", "done:b")
    expected = observability.make_trace_id("projZ")
    assert {s["trace_id"] for s in fake_lf.starts} == {expected}   # both jobs, one trace
    assert fake_lf.flushed == 2


class _Boom(Exception):
    """Sentinel error raised by an instrumented body."""


def test_generation_propagates_body_exception(fake_lf):
    """A body error inside generation() must surface as itself, not masked.

    Regression: when the wrapped body raised, the ``except Exception: yield None``
    branch did an illegal SECOND yield, turning every real LLM error into
    ``RuntimeError("generator didn't stop after throw()")`` and crashing the
    tail-frame pipeline.
    """
    with pytest.raises(_Boom):
        with observability.generation("agents-director-generate-motion", model="m"):
            raise _Boom("llm call failed")
    # The observation still recorded its exit (no leaked/open span).
    assert fake_lf._stack == []


def test_trace_propagates_body_exception(fake_lf):
    with pytest.raises(_Boom):
        with observability.trace("worker-shot-pipeline-run"):
            raise _Boom("job failed")
    assert fake_lf._stack == []


async def test_traced_job_records_return_value_as_output(fake_lf, monkeypatch):
    """The root job span must carry the task's return value as output (not undefined)."""
    async def fake_resolve(project_id):
        return "vm-session"

    monkeypatch.setattr(observability, "resolve_session_for", fake_resolve)

    summary = {"shot_id": 1, "status": "completed", "video_path": "/m/out.mp4"}

    @observability.traced_job("worker-shot-pipeline-run")
    async def job(ctx, project_id):
        return summary

    r = await job({}, "projOut")
    assert r == summary
    assert any(u.get("output") == summary for u in fake_lf.updates)


async def test_traced_job_records_error_on_failure(fake_lf, monkeypatch):
    async def fake_resolve(project_id):
        return "vm-session"

    monkeypatch.setattr(observability, "resolve_session_for", fake_resolve)

    @observability.traced_job("worker-shot-pipeline-run")
    async def job(ctx, project_id):
        raise _Boom("video provider exploded")

    with pytest.raises(_Boom):
        await job({}, "projErr")
    assert any(u.get("level") == "ERROR" for u in fake_lf.updates)


async def test_project_context_anchors_api_generation(fake_lf, monkeypatch):
    """API path: a generation opened inside project_context joins the project trace."""
    async def fake_resolve(project_id):
        return "vm-session"

    monkeypatch.setattr(observability, "resolve_session_for", fake_resolve)

    async with observability.project_context("projQ", "api-regenerate-motion"):
        with observability.generation("api-pipeline-regenerate-motion", model="m"):
            pass

    expected = observability.make_trace_id("projQ")
    assert {s["trace_id"] for s in fake_lf.starts} == {expected}
