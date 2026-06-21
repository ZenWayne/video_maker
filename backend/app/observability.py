"""Langfuse observability (LLM tracing) — thin, degrade-to-no-op wrapper.

All LLM/worker instrumentation goes through the helpers here so that, when
Langfuse is not configured (no keys / ``langfuse_enabled=False``), the whole
thing collapses to cheap no-ops and dev keeps working without credentials.

Conventions:
* Credentials come from settings (keys via secrets, host via config.yml).
* Env vars are set BEFORE the first ``get_client()`` call (the v3 singleton is
  lazy, so this is what actually wires the client up).
* Trace/generation names follow ``<feature>-<action>`` / ``<provider>-<model>``.
"""

import functools
import hashlib
import logging
import os
import sys
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Callable, Dict, Iterator, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

_enabled: Optional[bool] = None
_client = None


def make_session_id(project_id: str, created_at: Any = None) -> str:
    """One project → one stable session id: ``vm-<sha256(project_id)[:12]>-<ts>``.

    The hash keeps raw project ids out of the session label; the timestamp is the
    project's creation time (a fixed point), so the id is identical across every
    job and request for that project — they all chain into one session. Falls
    back to the hash alone when no timestamp is available.
    """
    digest = hashlib.sha256(str(project_id).encode()).hexdigest()[:12]
    if created_at is None:
        return f"vm-{digest}"
    try:
        ts = int(created_at.timestamp())  # datetime
    except AttributeError:
        try:
            ts = int(created_at)  # already an epoch int/str
        except (TypeError, ValueError):
            return f"vm-{digest}"
    return f"vm-{digest}-{ts}"


def make_trace_id(project_id: str) -> str:
    """Deterministic per-project Langfuse trace id (32-hex, W3C format).

    Because it is derived solely from ``project_id``, every worker job and API
    request for the project starts its root span in the SAME trace — so the
    whole project shows up as one chained trace, not many separate ones.
    """
    client = get_langfuse()
    if client is not None:
        try:
            return client.create_trace_id(seed=str(project_id))
        except Exception:  # pragma: no cover - defensive
            pass
    return hashlib.sha256(str(project_id).encode()).hexdigest()[:32]


async def resolve_session_for(project_id: str) -> Optional[str]:
    """Resolve the per-project session id, reading the project's ``created_at``.

    Returns ``None`` when tracing is disabled. Any lookup failure degrades to a
    timestamp-less (hash-only) id rather than breaking the caller.
    """
    if get_langfuse() is None:
        return None
    try:
        from sqlalchemy import select

        from app.db import AsyncSession as session_factory
        from app.models.project import Project

        async with session_factory() as s:
            row = await s.execute(
                select(Project.created_at).where(Project.id == project_id)
            )
            created_at = row.scalar_one_or_none()
        return make_session_id(project_id, created_at)
    except Exception:  # pragma: no cover - defensive
        return make_session_id(project_id, None)


@contextmanager
def session(
    session_id: Optional[str],
    tags: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Iterator[Optional[str]]:
    """Bind trace-level attributes for everything created inside this context.

    Uses Langfuse v4's ``propagate_attributes`` — the supported way to set
    ``session_id`` / ``tags`` / ``metadata`` so they propagate to ALL child
    observations, including API-triggered generations that have no root trace.
    Must be entered BEFORE the spans it should cover. No-op when disabled.
    """
    if get_langfuse() is None or not session_id:
        yield None
        return

    def _make():
        from langfuse import propagate_attributes

        return propagate_attributes(session_id=session_id, tags=tags, metadata=metadata)

    with _safe_observe(_make, session_id, "propagate_attributes") as obj:
        yield obj


def _resolve_enabled() -> bool:
    """Langfuse is on only when explicitly enabled AND both keys are present."""
    return bool(
        settings.langfuse_enabled
        and settings.langfuse_public_key
        and settings.langfuse_secret_key
    )


def is_enabled() -> bool:
    return _resolve_enabled()


def get_langfuse():
    """Return the Langfuse singleton, or ``None`` when tracing is disabled.

    Sets the SDK env vars (read lazily by ``get_client()``) on first use.
    Any import/init failure is swallowed and disables tracing — observability
    must never break the pipeline.
    """
    global _enabled, _client

    if _enabled is None:
        _enabled = _resolve_enabled()
        if _enabled:
            os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
            os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)
            os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)
            try:
                from langfuse import get_client

                _client = get_client()
                logger.info("Langfuse tracing enabled (host=%s)", settings.langfuse_host)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Langfuse init failed, tracing disabled: %s", exc)
                _enabled = False
                _client = None

    return _client


def _start_observation(client, name: str, as_type: str, **kwargs: Any):
    """Open an observation, tolerant of both SDK v4 and v3 method names.

    v4 unifies everything under ``start_as_current_observation(as_type=...)``;
    v3 also exposes ``start_as_current_span`` / ``start_as_current_generation``.
    """
    start_obs = getattr(client, "start_as_current_observation", None)
    if start_obs is not None:
        return start_obs(name=name, as_type=as_type, **kwargs)
    # v3 fallback
    if as_type == "generation":
        return client.start_as_current_generation(name=name, **kwargs)
    return client.start_as_current_span(name=name, **kwargs)


@contextmanager
def _safe_observe(make_cm: Callable[[], Any], fallback: Any, what: str) -> Iterator[Any]:
    """Enter a Langfuse context manager defensively, transparent to body errors.

    Observability must never break the pipeline, so failures in *Langfuse itself*
    (creating, entering, or exiting the observation) are swallowed and we fall
    back to ``fallback``. But the wrapped body's own exceptions are the caller's
    real errors — they propagate unchanged.

    NOTE: do not ``yield`` inside a ``try`` whose ``except`` yields again. A
    generator context manager must yield exactly once; a second yield after the
    body's exception is thrown in raises ``RuntimeError("generator didn't stop
    after throw()")``, masking the real error. Hence the manual enter/exit here.
    """
    try:
        cm = make_cm()
        obj = cm.__enter__()
    except Exception as exc:  # pragma: no cover - defensive (Langfuse setup)
        logger.warning("Langfuse %s failed, continuing untraced: %s", what, exc)
        yield fallback
        return
    try:
        yield obj
    except BaseException:
        # Body raised: let Langfuse record the failed exit, then re-raise the
        # ORIGINAL error regardless of what __exit__ returns or raises.
        try:
            cm.__exit__(*sys.exc_info())
        except Exception:  # pragma: no cover - defensive (Langfuse exit)
            pass
        raise
    else:
        try:
            cm.__exit__(None, None, None)
        except Exception as exc:  # pragma: no cover - defensive (Langfuse exit)
            logger.warning("Langfuse %s exit failed: %s", what, exc)


@contextmanager
def trace(name: str, trace_id: Optional[str] = None, **kwargs: Any) -> Iterator[Any]:
    """Root span context manager for a unit of work (a worker job).

    When ``trace_id`` is given the span is anchored to that exact trace, so all
    units of work sharing the id chain into one trace. No-op (yields ``None``)
    when disabled. ``kwargs`` forward to the observation (``input=``, etc.).
    """
    client = get_langfuse()
    if client is None:
        yield None
        return
    if trace_id:
        kwargs["trace_context"] = {"trace_id": trace_id}
    with _safe_observe(
        lambda: _start_observation(client, name, "span", **kwargs),
        None,
        f"trace {name!r}",
    ) as span:
        yield span


@contextmanager
def generation(
    name: str, model: str, trace_id: Optional[str] = None, **kwargs: Any
) -> Iterator[Any]:
    """LLM generation context manager (enables token/cost tracking in the UI).

    ``trace_id`` anchors an otherwise-rootless generation to a specific trace.
    No-op (yields ``None``) when tracing is disabled.
    """
    client = get_langfuse()
    if client is None:
        yield None
        return
    if trace_id:
        kwargs["trace_context"] = {"trace_id": trace_id}
    with _safe_observe(
        lambda: _start_observation(client, name, "generation", model=model, **kwargs),
        None,
        f"generation {name!r}",
    ) as gen:
        yield gen


@asynccontextmanager
async def _project_root(
    project_id: str,
    name: str,
    *,
    tags: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    input: Any = None,
) -> AsyncIterator[Any]:
    """The single place a project's session + root trace are bound together.

    Resolves the project's stable session id and deterministic trace id, binds
    the session (so it propagates to every child observation) and opens the root
    span. BOTH entry points — :func:`project_context` (API) and
    :func:`traced_job` (worker) — go through here, so all of a project's work
    chains into the same session and trace. Yields ``(span, trace_id)``, or
    ``(None, None)`` when tracing is disabled.
    """
    if get_langfuse() is None:
        yield None, None
        return
    session_id = await resolve_session_for(project_id)
    trace_id = make_trace_id(project_id)
    meta = {"project_id": project_id, **(metadata or {})}
    with session(session_id, tags=tags, metadata=meta), trace(
        name, trace_id=trace_id, input=input
    ) as span:
        yield span, trace_id


@asynccontextmanager
async def project_context(project_id: str, name: str) -> AsyncIterator[Optional[str]]:
    """Per-project root span for an API request (regenerate, edit, rewrite).

    Thin wrapper over :func:`_project_root`; API-triggered generations opened
    inside it join the very same trace and session as the project's worker jobs.
    Yields the project trace id (``None`` when disabled).
    """
    async with _project_root(
        project_id, name, input={"project_id": project_id}
    ) as (_span, trace_id):
        yield trace_id


def update_span(span: Any, **kwargs: Any) -> None:
    """Update a span/generation returned by the context managers above."""
    if span is None:
        return
    try:
        span.update(**kwargs)
    except Exception:  # pragma: no cover - defensive
        pass


def traced_job(name: str, tags: Optional[List[str]] = None) -> Callable:
    """Decorator for arq worker tasks: wrap the whole job in a project root trace.

    The task signature is ``(ctx, project_id, ...)``. Delegates session + trace
    binding to :func:`_project_root` (so every nested generation chains into the
    project's session); kwargs (``shot_id``, ``actor``) land in trace metadata,
    the return value / errors are recorded on the root span, and buffered events
    are flushed when the job returns. No-op when tracing is disabled.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(ctx: Dict[str, Any], project_id: str, *args: Any, **kwargs: Any):
            if get_langfuse() is None:
                return await func(ctx, project_id, *args, **kwargs)
            try:
                async with _project_root(
                    project_id,
                    name,
                    tags=(tags or []) + ["worker"],
                    metadata=kwargs,
                    input={"project_id": project_id, "args": list(args), **kwargs},
                ) as (span, _trace_id):
                    try:
                        result = await func(ctx, project_id, *args, **kwargs)
                    except Exception as exc:
                        update_span(span, level="ERROR", status_message=str(exc))
                        raise
                    update_span(span, output=result)
                    return result
            finally:
                flush()

        return wrapper

    return decorator


def flush() -> None:
    """Flush buffered events — call before a short-lived process exits.

    arq worker jobs run in a long-lived process, but flushing at the end of
    each job guarantees traces land promptly and survive a worker restart.
    """
    client = get_langfuse()
    if client is None:
        return
    try:
        client.flush()
    except Exception:  # pragma: no cover - defensive
        pass
