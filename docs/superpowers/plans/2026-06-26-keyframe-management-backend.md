# Keyframe Management — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Scope:** BACKEND ONLY. The frontend keyframe control is a **layered multi-select dropdown** whose exact layout is pending a Pencil mock — write a separate frontend plan after that mock. This plan delivers all backend behavior the frontend will call.
>
> **Read first:** `docs/superpowers/specs/2026-06-26-keyframe-management-design.md` — it is the source of truth. Line numbers below are from master and MUST be re-verified before editing (grep for the quoted code).

**Goal:** Make keyframe usage path-driven (presence of `custom_first_frame_path` / `target_last_frame_path` = use it), fix the regenerate-resurrects-tail-frame bug, and add upload/delete/extract endpoints (ts_uuid naming) plus explicit first-frame continuity initialization.

**Architecture:** Drop `tf_confirmed`/`skip_tail_frame` from every generation/regeneration decision (path-presence is the single source of truth); keep `tf_status` only as a transient generating/done/failed indicator. Add a `ts_uuid_name` storage helper and five thin endpoints (upload first/tail, delete first, extract first/tail). Make the implicit first-frame resolution chain explicit by writing `custom_first_frame_path` eagerly (shot 1 = character ref; continuous shot N = previous shot's last frame after it generates).

**Tech Stack:** Python / FastAPI / SQLAlchemy async; pytest (`asyncio_mode=auto`). ffmpeg/ffprobe and AI generators are **mocked** in tests.

## Global Constraints

- Run Python only via `uv` (`uv run --project backend pytest ...`); never `python`/`python3` directly.
- Mock all AI/model/generator calls; never run real ffmpeg/ffprobe in tests.
- No hardcoded absolute paths.
- **Path-presence is the only keyframe-use decision.** No code in the generate / regenerate / upload / delete / extract paths may read `tf_confirmed` or `skip_tail_frame`. `tf_status` stays transient (generating/done/failed) and is never a use-decision.
- `ts_uuid_name` format exactly `^\d+_[0-9a-f]{8}\.png$` (unix seconds `_` 8-hex-uuid).
- Frame fields: 首帧 = `custom_first_frame_path`, 尾帧 = `target_last_frame_path`. The extracted `first_frame_path` / `last_frame_path` are read-only results — extract COPIES them, never moves/deletes.
- Material-file audit (CLAUDE.md): downstream reads tail frame via the `shot.target_last_frame_path` DB field, never a hardcoded filename — keep it that way.

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `backend/app/services/storage.py` | Add `ts_uuid_name()` helper | Modify |
| `backend/worker/tasks.py` | Path-only tail decision; drop `tf_confirmed` reuse gate; first-frame continuity write | Modify |
| `backend/app/api/pipeline.py` | Fix regenerate; simplify delete-tail-frame/`_reset_tail_frame`; 5 new endpoints | Modify |
| `backend/tests/unit/…` | Unit tests per task | Create/Modify |

---

## Task 1: `ts_uuid_name` storage helper

**Files:**
- Modify: `backend/app/services/storage.py` (add helper; ensure `import time, uuid` present)
- Test: `backend/tests/unit/test_storage_ts_uuid.py` (create)

**Interfaces:**
- Produces: `ts_uuid_name(ext: str = ".png") -> str` → `f"{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"`.

- [ ] **Step 1: Failing test** — create `backend/tests/unit/test_storage_ts_uuid.py`:

```python
import re
from app.services import storage


def test_ts_uuid_name_format():
    name = storage.ts_uuid_name()
    assert re.fullmatch(r"\d+_[0-9a-f]{8}\.png", name), name


def test_ts_uuid_name_unique():
    assert storage.ts_uuid_name() != storage.ts_uuid_name()


def test_ts_uuid_name_custom_ext():
    assert storage.ts_uuid_name(".jpg").endswith(".jpg")
```

- [ ] **Step 2: Run, expect fail**
Run: `uv run --project backend pytest backend/tests/unit/test_storage_ts_uuid.py -v`
Expected: FAIL (`AttributeError: ... has no attribute 'ts_uuid_name'`)

- [ ] **Step 3: Implement** — in `storage.py` (top has `import` block; add `time`/`uuid` if missing), add:

```python
import time
import uuid


def ts_uuid_name(ext: str = ".png") -> str:
    """Timestamped unique filename: ``<unix_seconds>_<8hex>.<ext>``.

    Each call is unique, so user-uploaded/extracted keyframes get a fresh URL
    and the browser never serves a cached stale frame.
    """
    return f"{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
```

- [ ] **Step 4: Run, expect pass**
Run: `uv run --project backend pytest backend/tests/unit/test_storage_ts_uuid.py -v` → 3 passed.

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/storage.py backend/tests/unit/test_storage_ts_uuid.py
git commit -m "feat(storage): add ts_uuid_name helper for unique keyframe filenames"
```

---

## Task 2: Worker tail-frame decision = path presence only

**Files:**
- Modify: `backend/worker/tasks.py` — the tail-frame resolution (~`355-360`) and the regenerate-reuse gate (~`304`)
- Test: `backend/tests/unit/test_tail_frame_decision.py` (create)

**Interfaces:**
- Consumes: `Shot.target_last_frame_path`, `Shot.motion_prompt`, `Shot.first_frame_path`.
- Produces: tail frame is passed to generation iff `target_last_frame_path` is set and the file exists — independent of `tf_confirmed`/`skip_tail_frame`.

**Before editing:** `grep -n "tf_confirmed" backend/worker/tasks.py` and confirm the two sites (reuse gate + tail decision). Extract the decision into a tiny pure helper so it is unit-testable without running the whole task:

- [ ] **Step 1: Failing test** — create `backend/tests/unit/test_tail_frame_decision.py`:

```python
from pathlib import Path
from app.worker import tasks  # adjust import to where resolve_tail_frame lives


def test_tail_used_when_path_present(tmp_path):
    f = tmp_path / "t.png"; f.write_bytes(b"x")
    assert tasks.resolve_tail_frame(str(f)) == str(f)


def test_tail_none_when_path_empty():
    assert tasks.resolve_tail_frame(None) is None


def test_tail_none_when_file_missing(tmp_path):
    assert tasks.resolve_tail_frame(str(tmp_path / "missing.png")) is None
```

> Adjust the import path to match the module (`backend/worker/tasks.py` → `worker.tasks` or `app...`; verify how other tests import it).

- [ ] **Step 2: Run, expect fail** (`resolve_tail_frame` undefined).

- [ ] **Step 3: Implement** — add helper near the tail logic in `tasks.py`:

```python
def resolve_tail_frame(target_last_frame_path: str | None) -> str | None:
    """Tail frame is used iff its path is set and the file exists.

    Path presence is the single source of truth — tf_confirmed/skip_tail_frame
    are intentionally NOT consulted.
    """
    if target_last_frame_path:
        p = Path(target_last_frame_path)
        if p.exists():
            return str(p)
    return None
```

Replace the existing block (~355-360):
```python
last_frame = None
if shot.tf_confirmed and shot.target_last_frame_path:
    tf_path = Path(shot.target_last_frame_path)
    if tf_path.exists():
        last_frame = str(tf_path)
```
with:
```python
last_frame = resolve_tail_frame(shot.target_last_frame_path)
```

And at ~`304`, change `if shot.tf_confirmed and shot.motion_prompt and shot.first_frame_path:` to drop the `tf_confirmed` term:
```python
if shot.motion_prompt and shot.first_frame_path:
```

- [ ] **Step 4: Run, expect pass.** Also `grep -n "tf_confirmed\|skip_tail_frame" backend/worker/tasks.py` → no remaining decision reads (only writes, if any, are fine to leave but prefer removing).

- [ ] **Step 5: Commit**
```bash
git add backend/worker/tasks.py backend/tests/unit/test_tail_frame_decision.py
git commit -m "refactor(worker): tail-frame use is path-presence only (drop tf_confirmed)"
```

---

## Task 3: Fix regenerate resurrecting the tail frame

**Files:**
- Modify: `backend/app/api/pipeline.py` `regenerate_shots` (~`305-373`, specifically the `has_valid_tail_frame` block `~344-366`)
- Test: `backend/tests/unit/test_regenerate_tail_frame.py` (create)

**Interfaces:**
- Produces: regenerate no longer writes `tf_confirmed`/`skip_tail_frame`/`tf_status="done"` based on file existence; `target_last_frame_path` is left exactly as stored (None stays None).

- [ ] **Step 1: Failing test** — create `backend/tests/unit/test_regenerate_tail_frame.py`. Mirror an existing pipeline unit/integration test's session+project+shot fixture (grep `regenerate` in `backend/tests`). Assert:
  - shot with `target_last_frame_path=None` → after regenerate, still `None` (not resurrected).
  - shot with `target_last_frame_path=<path>` → after regenerate, path unchanged and the handler did not force `tf_confirmed`.

  (Write the test against the real handler the way the existing regenerate tests do; if none exist, drive it through the FastAPI test client with AI dispatch mocked per CLAUDE.md.)

- [ ] **Step 2: Run, expect fail.**

- [ ] **Step 3: Implement** — delete the `has_valid_tail_frame` branch that sets `tf_status="done"` / `tf_confirmed=True` / clears on missing. Replace with: leave `target_last_frame_path` untouched; only set `tf_status="generating"` if (and where) this regenerate actually re-triggers tail-frame generation. Verify with `grep -n "has_valid_tail_frame\|tf_confirmed" backend/app/api/pipeline.py`.

- [ ] **Step 4: Run, expect pass.**

- [ ] **Step 5: Commit**
```bash
git add backend/app/api/pipeline.py backend/tests/unit/test_regenerate_tail_frame.py
git commit -m "fix(regenerate): stop resurrecting tail frame (path is source of truth)"
```

---

## Task 4: Simplify delete-tail-frame / `_reset_tail_frame`

**Files:**
- Modify: `backend/app/api/pipeline.py` — `_reset_tail_frame` (~`51-61`) and `delete_tail_frame` (~`914-959`)
- Test: extend `backend/tests/unit/test_regenerate_tail_frame.py` or a new `test_delete_tail_frame.py`

**Interfaces:**
- Produces: deleting a tail frame clears `target_last_frame_path` (+ unlink) and `tf_status=None`; it MUST NOT set `skip_tail_frame`.

- [ ] **Step 1: Failing test** — assert after `delete-tail-frame`: `target_last_frame_path is None`, file gone, `tf_status is None`, and `skip_tail_frame` is NOT set to True by this path.
- [ ] **Step 2: Run, expect fail.**
- [ ] **Step 3: Implement** — in `_reset_tail_frame` drop the `skip` parameter / `skip_tail_frame` assignment; keep clearing `target_last_frame_path`, `tf_status`, `tf_error_message`. Update `delete_tail_frame` callers accordingly. `grep -n "skip_tail_frame" backend/app/api/pipeline.py` → none remaining.
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit**
```bash
git add backend/app/api/pipeline.py backend/tests/unit/
git commit -m "refactor(tail-frame): delete just clears path+tf_status (drop skip_tail_frame)"
```

---

## Task 5: Upload first/tail frame endpoints (ts_uuid)

**Files:**
- Modify: `backend/app/api/pipeline.py` (two new routes, near the other shot routes; reuse existing `UploadFile`, `_require_user`, `get_session`, `Shot`, `_get_project_or_404`, `shot_custom_frames_dir`, `shot_dir`/`shot_target_last_frame_path`’s parent, `to_media_url`)
- Test: `backend/tests/unit/test_upload_keyframes.py` (create)

**Interfaces:**
- `POST /projects/{project_id}/shots/{shot_id}/upload-first-frame` (multipart `file`) → saves `shot_custom_frames_dir/ts_uuid` → sets `custom_first_frame_path`; returns `{shot_id, custom_first_frame_path: <media_url>}`.
- `POST /projects/{project_id}/shots/{shot_id}/upload-tail-frame` (multipart `file`) → saves `<shot dir>/ts_uuid` → sets `target_last_frame_path`, `tf_status="done"`; returns `{shot_id, target_last_frame_path: <media_url>, tf_status}`.

- [ ] **Step 1: Failing tests** — post a small PNG bytes payload to each; assert the field is set to a path matching `.*/\d+_[0-9a-f]{8}\.png`, the file exists on disk, and (tail) `tf_status=="done"`. Mock nothing AI here (pure file IO). Use the FastAPI test client + `X-User-Name` header (see existing upload tests, e.g. `reference-images`).
- [ ] **Step 2: Run, expect fail.**
- [ ] **Step 3: Implement** — model on the existing `upload_shot_references` handler (`pipeline.py:1005`). For first frame:

```python
@router.post("/projects/{project_id}/shots/{shot_id}/upload-first-frame")
async def upload_first_frame(project_id: str, shot_id: int,
                             file: UploadFile = File(...),
                             user: str = Depends(_require_user),
                             session: AsyncSession = Depends(get_session)):
    await _get_project_or_404(project_id, session)
    shot = (await session.execute(select(Shot).where(
        Shot.project_id == project_id, Shot.shot_id == shot_id))).scalar_one_or_none()
    if not shot:
        raise HTTPException(404, "Shot not found")
    dest_dir = shot_custom_frames_dir(project_id, shot_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / ts_uuid_name(Path(file.filename or ".png").suffix or ".png")
    dest.write_bytes(await file.read())
    shot.custom_first_frame_path = str(dest)
    await session.commit()
    return {"shot_id": shot_id, "custom_first_frame_path": to_media_url(str(dest))}
```

Tail frame: same shape but write into the shot directory (parent of `shot_target_last_frame_path(project_id, shot_id)`), set `target_last_frame_path` + `tf_status="done"`, return those.

- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit**
```bash
git add backend/app/api/pipeline.py backend/tests/unit/test_upload_keyframes.py
git commit -m "feat(api): upload-first-frame / upload-tail-frame (ts_uuid naming)"
```

---

## Task 6: Delete-first-frame endpoint

**Files:**
- Modify: `backend/app/api/pipeline.py`
- Test: `backend/tests/unit/test_delete_first_frame.py` (create)

**Interfaces:**
- `DELETE /projects/{project_id}/shots/{shot_id}/first-frame` → unlink the `custom_first_frame_path` file (if present), set field `None`; returns `{shot_id, custom_first_frame_path: null}`.

- [ ] **Step 1: Failing test** — seed a shot with a real temp file as `custom_first_frame_path`; DELETE; assert field `None` and file removed.
- [ ] **Step 2: Run, expect fail.**
- [ ] **Step 3: Implement** — load shot, `Path(shot.custom_first_frame_path).unlink(missing_ok=True)` when set, set `None`, commit.
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit**
```bash
git add backend/app/api/pipeline.py backend/tests/unit/test_delete_first_frame.py
git commit -m "feat(api): delete first-frame config (clear path + unlink)"
```

---

## Task 7: Extract-current-frame endpoints

**Files:**
- Modify: `backend/app/api/pipeline.py`
- Test: `backend/tests/unit/test_extract_keyframes.py` (create)

**Interfaces:**
- `POST /.../extract-first-frame` → COPY `first_frame_path` → `shot_custom_frames_dir/ts_uuid` → set `custom_first_frame_path`; 400 if `first_frame_path` missing/absent. Returns `{shot_id, custom_first_frame_path: <media_url>}`.
- `POST /.../extract-last-frame` → COPY `last_frame_path` → `<shot dir>/ts_uuid` → set `target_last_frame_path`, `tf_status="done"`. Returns `{shot_id, target_last_frame_path, tf_status}`.

- [ ] **Step 1: Failing tests** — seed shot with real temp `first_frame_path`/`last_frame_path` files; POST each; assert NEW ts_uuid file created (distinct path), config field points to it, SOURCE file still exists, and missing-source → 400.
- [ ] **Step 2: Run, expect fail.**
- [ ] **Step 3: Implement** — use `shutil.copy2(src, dest)` where `dest = <dir> / ts_uuid_name(".png")`; raise `HTTPException(400, ...)` if source path empty or `not Path(src).exists()`.
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit**
```bash
git add backend/app/api/pipeline.py backend/tests/unit/test_extract_keyframes.py
git commit -m "feat(api): extract current shot first/last frame into keyframe config"
```

---

## Task 8: Explicit first-frame continuity initialization

**Files:**
- Modify: `backend/worker/tasks.py` (after a shot's `last_frame_path` is written, fill next continuous shot's `custom_first_frame_path`) and the shot-creation path (shot 1 ← first character ref)
- Test: `backend/tests/unit/test_first_frame_init.py` (create)

**Interfaces:**
- Consumes: `_get_first_character_ref` (`tasks.py:561`), prev/next shot lookup by `shot_id`.
- Produces: shot 1 gets `custom_first_frame_path` = first character ref at creation (if not already set); after shot N generates, the next continuous shot (N+1, per `use_prev_last_frame`/`align_with_previous`) gets `custom_first_frame_path` = shot N's `last_frame_path`.

> **Before implementing:** grep how/where shots are created from the script (`grep -n "Shot(" backend/app/api/pipeline.py backend/worker/tasks.py`) and where `last_frame_path` is set post-generation (`tasks.py:333`, and the generation completion site ~`391-401`). Confirm the "next continuous shot" predicate from `use_prev_last_frame`/`align_with_previous` usage. If the creation site or continuity predicate is ambiguous, STOP and ask before guessing.

- [ ] **Step 1: Failing tests** — (a) creating shots → shot 1’s `custom_first_frame_path` == first character ref path; (b) after shot N generation writes `last_frame_path`, the next continuous shot’s `custom_first_frame_path` == that `last_frame_path`. Mock the generator/ffmpeg; assert only the DB field writes.
- [ ] **Step 2: Run, expect fail.**
- [ ] **Step 3: Implement** the two writes. Do not overwrite a `custom_first_frame_path` the user explicitly set via upload/extract during the same run unless continuity should win — default: continuity fill writes the next shot’s field (document the choice in the commit body).
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit**
```bash
git add backend/worker/tasks.py backend/tests/unit/test_first_frame_init.py
git commit -m "feat(shots): explicit first-frame continuity init (shot1=char ref, next=prev last frame)"
```

---

## Final backend checks (before frontend plan)

- [ ] `grep -rn "tf_confirmed\|skip_tail_frame" backend/app backend/worker` → only dormant/vestigial reads remain, none in generate/regenerate/upload/delete/extract decisions. List any survivors and justify or remove.
- [ ] Full backend suite: `uv run --project backend pytest backend/tests -q` green.
- [ ] Manual smoke (stack up): `POST upload-first-frame` / `extract-last-frame` on a real shot → field set to a ts_uuid path; regenerate a shot whose `target_last_frame_path` is None → generation does NOT use a tail frame.

## Self-Review (author ran)

- **Spec coverage:** path-as-truth (T2), regenerate fix (T3), delete simplification (T4), ts_uuid (T1) + upload (T5) + delete-first (T6) + extract (T7), continuity init (T8), tf_status kept transient (T2/T3), field-removal-from-decisions check (Final). ✓
- **Placeholders:** the few "mirror the existing test fixture / grep to confirm the site" notes are deliberate — line numbers are from a sibling worktree and MUST be verified on master; they are scoped pointers, not unfilled logic. The novel code (helper, decision, endpoints) is given inline.
- **Type/name consistency:** `ts_uuid_name`, `resolve_tail_frame`, field names (`custom_first_frame_path`, `target_last_frame_path`), and the response keys are consistent across tasks.
- **Frontend:** intentionally excluded — depends on the Pencil "layered multi-select dropdown" mock; write its own plan after the mock, wiring the endpoints defined here.
