# Auto Silence Trim (Tail) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a suggest-only "静音裁剪" (silence trim) button to the trim dialog that detects trailing silence, moves the slider to a suggested end frame, and lets the user preview before confirming with the existing trim flow.

**Architecture:** A new read-only backend service function `suggest_silence_trim()` wraps the existing `detect_speech_end()` to compute a suggested keep-frame count (silence onset + 3-frame padding). A new read-only endpoint `POST /detect-silence` exposes it (no file writes, no status resets). The frontend adds a button that calls the endpoint and only moves the slider (`setEndFrame`); the actual trim + downstream state reset is unchanged and still happens via the existing 确认裁剪 → `trim` endpoint.

**Tech Stack:** Python / FastAPI / SQLAlchemy (async) backend; React + TypeScript (Vite) frontend; pytest (asyncio_mode=auto) for backend; Playwright for e2e. ffmpeg/ffprobe are invoked by existing helpers and are **mocked** in tests.

## Global Constraints

- **Run Python via `uv`**, project pinned: backend tests run with `uv run --project backend pytest ...`. Never call `python`/`python3` directly. (verbatim from project rules)
- **Mock all AI/model calls and never run real ffmpeg/ffprobe in tests** — unit tests patch `detect_speech_end` / `get_video_info`; Playwright tests mock the `detect-silence` endpoint with `route.fulfill`.
- **No hardcoded absolute paths** anywhere (code, config, tests). Use relative/dynamic resolution.
- **Silence-detection parameters stay backend constants**, not user-tunable: threshold `-30dB`, min silence `0.3s` (inherited from `detect_speech_end` defaults), tail padding `SILENCE_TAIL_PADDING_FRAMES = 3`, min keep frames `24` (matches `TrimDialog` `minFrames`).
- **`detect-silence` endpoint must have zero material-file side effects**: it must NOT write/rename/delete any shot file, must NOT touch backups, must NOT reset `cc_status`/`vc_status`, must NOT re-extract `last_frame`.

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `backend/app/agents/video_trimmer.py` | Add `SILENCE_TAIL_PADDING_FRAMES`, `MIN_TRIM_FRAMES`, `suggest_silence_trim()` (read-only frame math) | Modify |
| `backend/app/api/pipeline.py` | Add `POST /detect-silence` endpoint (read-only) | Modify |
| `backend/tests/unit/test_video_trimmer.py` | Unit tests for `suggest_silence_trim` | Modify |
| `frontend-vite/src/lib/api.ts` | Add `detectSilence()` client method | Modify |
| `frontend-vite/src/components/TrimDialog.tsx` | Add 静音裁剪 button + handler (slider-only, no apply) | Modify |
| `tests/e2e/auto-silence-trim.spec.ts` | Playwright e2e: mocked endpoint, slider moves, no-silence notice | Create |

---

## Task 1: Backend — `suggest_silence_trim()` service function

**Files:**
- Modify: `backend/app/agents/video_trimmer.py` (add constants near top after imports; add function after `auto_trim_to_speech_end`, ~line 275)
- Test: `backend/tests/unit/test_video_trimmer.py`

**Interfaces:**
- Consumes: existing `detect_speech_end(video_path) -> float | None` and `get_video_info(video_path) -> {"fps","total_frames","duration"}` (same module).
- Produces:
  - `SILENCE_TAIL_PADDING_FRAMES: int = 3`
  - `MIN_TRIM_FRAMES: int = 24`
  - `suggest_silence_trim(video_path: str, padding_frames: int = SILENCE_TAIL_PADDING_FRAMES) -> dict | None` returning `{"suggested_end_frame": int, "silence_start_time": float, "fps": float, "total_frames": int, "duration": float}` or `None` when there is no trailing silence / nothing to trim.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/unit/test_video_trimmer.py`:

```python
def test_suggest_silence_trim_returns_suggested_frame():
    """Trailing silence at 2.0s, 25fps → 50 + 3 padding = keep 53 frames."""
    with patch.object(vt, "detect_speech_end", return_value=2.0), \
         patch.object(vt, "get_video_info", return_value={"fps": 25.0, "total_frames": 200, "duration": 8.0}):
        result = vt.suggest_silence_trim("/fake/out.mp4")

    assert result == {
        "suggested_end_frame": 53,
        "silence_start_time": 2.0,
        "fps": 25.0,
        "total_frames": 200,
        "duration": 8.0,
    }


def test_suggest_silence_trim_no_trailing_silence_returns_none():
    """No trailing silence → nothing to suggest."""
    with patch.object(vt, "detect_speech_end", return_value=None):
        result = vt.suggest_silence_trim("/fake/out.mp4")

    assert result is None


def test_suggest_silence_trim_clamps_to_min_frames():
    """Suggested frame below the 24-frame floor is clamped up to 24."""
    with patch.object(vt, "detect_speech_end", return_value=0.2), \
         patch.object(vt, "get_video_info", return_value={"fps": 25.0, "total_frames": 200, "duration": 8.0}):
        result = vt.suggest_silence_trim("/fake/out.mp4")

    # 0.2*25=5, +3=8 → clamped up to 24
    assert result["suggested_end_frame"] == 24


def test_suggest_silence_trim_suggestion_at_or_past_end_returns_none():
    """Silence onset + padding reaches the last frame → nothing to trim."""
    with patch.object(vt, "detect_speech_end", return_value=7.95), \
         patch.object(vt, "get_video_info", return_value={"fps": 25.0, "total_frames": 200, "duration": 8.0}):
        result = vt.suggest_silence_trim("/fake/out.mp4")

    # 7.95*25=198.75→round 199, +3=202 >= 200 → None
    assert result is None


def test_suggest_silence_trim_tiny_video_clamp_then_bounds_returns_none():
    """Clamp-to-24 must still respect total_frames: 24 >= total → None."""
    with patch.object(vt, "detect_speech_end", return_value=0.2), \
         patch.object(vt, "get_video_info", return_value={"fps": 25.0, "total_frames": 20, "duration": 0.8}):
        result = vt.suggest_silence_trim("/fake/out.mp4")

    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project backend pytest backend/tests/unit/test_video_trimmer.py -k suggest_silence_trim -v`
Expected: FAIL with `AttributeError: module 'app.agents.video_trimmer' has no attribute 'suggest_silence_trim'`

- [ ] **Step 3: Add constants and implementation**

Near the top of `backend/app/agents/video_trimmer.py`, after `logger = logging.getLogger(__name__)` (line 13), add:

```python
# Tail silence trim — suggest-only (preview before confirm)
SILENCE_TAIL_PADDING_FRAMES = 3   # keep this many frames after speech ends (呼吸感)
MIN_TRIM_FRAMES = 24              # floor; mirrors TrimDialog minFrames
```

After `auto_trim_to_speech_end` (end of file, ~line 275), add:

```python
def suggest_silence_trim(
    video_path: str,
    padding_frames: int = SILENCE_TAIL_PADDING_FRAMES,
) -> dict | None:
    """Suggest a tail-trim point based on trailing silence — read-only.

    Mirrors ``auto_trim_to_speech_end`` but does NOT touch any file: it only
    computes the frame count to keep so the frontend can move the slider and
    let the user preview before confirming via the normal trim endpoint.

    Returns a dict with ``suggested_end_frame``, ``silence_start_time`` and
    video info, or ``None`` when there is no trailing silence / nothing to trim.
    """
    speech_end = detect_speech_end(video_path)
    if speech_end is None:
        return None

    info = get_video_info(video_path)
    suggested = round(speech_end * info["fps"]) + padding_frames
    if suggested < MIN_TRIM_FRAMES:
        suggested = MIN_TRIM_FRAMES
    if suggested >= info["total_frames"]:
        return None

    return {
        "suggested_end_frame": suggested,
        "silence_start_time": speech_end,
        **info,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --project backend pytest backend/tests/unit/test_video_trimmer.py -k suggest_silence_trim -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/agents/video_trimmer.py backend/tests/unit/test_video_trimmer.py
git commit -m "feat(trim): suggest_silence_trim — read-only tail-silence frame suggestion"
```

---

## Task 2: Backend — `POST /detect-silence` endpoint

**Files:**
- Modify: `backend/app/api/pipeline.py` (add new route after `align_tail_frame`, which ends ~line 1380)
- Test: `backend/tests/unit/test_video_trimmer.py` is unit-only; the endpoint is verified manually (see Step 4) and via the Playwright mock in Task 5. No new heavy integration test (would require a real video fixture + ffmpeg, which is disallowed).

**Interfaces:**
- Consumes: `suggest_silence_trim(video_path) -> dict | None` and `get_video_info(video_path) -> dict` from Task 1; existing helpers `_get_project_or_404`, `_require_user`, `get_session`, `Shot`, `HTTPException`, `select`.
- Produces: `POST /projects/{project_id}/shots/{shot_id}/detect-silence` returning
  `{"has_silence": bool, "suggested_end_frame": int | None, "silence_start_time": float | None, "fps": float, "total_frames": int, "duration": float}`.

- [ ] **Step 1: Add the endpoint**

In `backend/app/api/pipeline.py`, immediately after the `align_tail_frame` function (after its final `return {...}` block, ~line 1380), add:

```python
@router.post("/projects/{project_id}/shots/{shot_id}/detect-silence")
async def detect_silence(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Suggest a tail-trim point from trailing silence — read-only, no file writes.

    Returns a suggested end frame for the frontend to preview; the actual trim
    is performed later by the existing ``/trim`` endpoint when the user confirms.
    """
    from app.agents.video_trimmer import suggest_silence_trim, get_video_info

    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot or not shot.video_path:
        raise HTTPException(status_code=404, detail="Shot or video not found")

    suggestion = suggest_silence_trim(shot.video_path)
    if suggestion is None:
        return {
            "has_silence": False,
            "suggested_end_frame": None,
            "silence_start_time": None,
            **get_video_info(shot.video_path),
        }
    return {"has_silence": True, **suggestion}
```

- [ ] **Step 2: Verify import availability**

Confirm `Depends`, `AsyncSession`, `get_session`, `_require_user`, `_get_project_or_404`, `select`, `Shot`, `HTTPException` are already imported/defined in `pipeline.py` (they are used by the adjacent `align_tail_frame`/`restore_trim` handlers).

Run: `uv run --project backend python -c "import ast,sys; ast.parse(open('backend/app/api/pipeline.py').read()); print('parse OK')"`
Expected: `parse OK`

- [ ] **Step 3: Boot-check the route is registered**

Run:
```bash
uv run --project backend python -c "from app.api.pipeline import router; print([r.path for r in router.routes if 'detect-silence' in r.path])"
```
Expected: `['/projects/{project_id}/shots/{shot_id}/detect-silence']`

- [ ] **Step 4: Commit**

```bash
git add backend/app/api/pipeline.py
git commit -m "feat(api): POST /detect-silence — read-only tail-silence suggestion endpoint"
```

---

## Task 3: Frontend — `detectSilence()` API client method

**Files:**
- Modify: `frontend-vite/src/lib/api.ts` (add after `alignTailFrame`, ~line 337)

**Interfaces:**
- Consumes: existing `request('POST', url)` helper used by sibling methods.
- Produces: `api.detectSilence(projectId: string, shotId: number) => Promise<{ has_silence: boolean; suggested_end_frame: number | null; silence_start_time: number | null; fps: number; total_frames: number; duration: number }>`.

- [ ] **Step 1: Add the client method**

In `frontend-vite/src/lib/api.ts`, after the `alignTailFrame` method (closes at line 337), add:

```typescript
  // 静音裁剪建议（只读，返回建议帧，不实际裁剪）
  detectSilence: (
    projectId: string,
    shotId: number
  ): Promise<{ has_silence: boolean; suggested_end_frame: number | null; silence_start_time: number | null; fps: number; total_frames: number; duration: number }> => {
    return request('POST', `/api/projects/${projectId}/shots/${shotId}/detect-silence`)
  },
```

- [ ] **Step 2: Typecheck**

Run: `cd frontend-vite && npx tsc --noEmit`
Expected: no errors (exit 0)

- [ ] **Step 3: Commit**

```bash
git add frontend-vite/src/lib/api.ts
git commit -m "feat(api-client): add detectSilence()"
```

---

## Task 4: Frontend — 静音裁剪 button in TrimDialog (slider-only)

**Files:**
- Modify: `frontend-vite/src/components/TrimDialog.tsx`

**Interfaces:**
- Consumes: `api.detectSilence()` (Task 3); existing state setters `setEndFrame`, `setError`; existing `endFrame`/`isTrimming`/`isAligning`/`isRestoring`/`isPreviewing` state; `minFrames` (24); existing `seekToFrame`.
- Produces: a new button rendered in the left actions group; new state `isDetectingSilence`; new state `notice` for the neutral "无尾部静音可裁剪" message. No change to apply/confirm flow.

- [ ] **Step 1: Add the icon import**

In `TrimDialog.tsx` line 2, add `AudioLines` to the lucide import:

```typescript
import { Loader2, ChevronLeft, ChevronRight, Play, Square, Undo2, Crosshair, AudioLines } from 'lucide-react'
```

- [ ] **Step 2: Add state**

After `const [isAligning, setIsAligning] = useState(false)` (line 42), add:

```typescript
  const [isDetectingSilence, setIsDetectingSilence] = useState(false)
  const [notice, setNotice] = useState('')
```

- [ ] **Step 3: Add the handler**

After `handleAlignTailFrame` (closes at line 219), add. Note this handler **does not close the dialog and does not apply** — it only moves the slider:

```typescript
  const handleDetectSilence = async () => {
    setIsDetectingSilence(true)
    setError('')
    setNotice('')
    try {
      const result = await api.detectSilence(projectId, shot.shot_id)
      if (result.has_silence && result.suggested_end_frame != null) {
        setEndFrame(result.suggested_end_frame)
        seekToFrame(result.suggested_end_frame)
      } else {
        setNotice('无尾部静音可裁剪')
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Silence detect failed')
    } finally {
      setIsDetectingSilence(false)
    }
  }
```

- [ ] **Step 4: Render the button**

In the left actions group, after the 智能校准 button block (the `{shot.target_last_frame_path && (...)}` block closing at line 364), add a new button. It is always available (no `target_last_frame_path` gate):

```tsx
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleDetectSilence}
                  disabled={isDetectingSilence || isTrimming || isAligning || isRestoring || isPreviewing}
                >
                  {isDetectingSilence ? (
                    <><Loader2 className="w-4 h-4 mr-1 animate-spin" />检测中...</>
                  ) : (
                    <><AudioLines className="w-4 h-4 mr-1" />静音裁剪</>
                  )}
                </Button>
```

- [ ] **Step 5: Render the neutral notice**

Replace the existing error block (lines 383-385):

```tsx
            {error && (
              <p className="text-sm text-red-500">{error}</p>
            )}
```

with:

```tsx
            {error && (
              <p className="text-sm text-red-500">{error}</p>
            )}
            {notice && !error && (
              <p className="text-sm text-zinc-500">{notice}</p>
            )}
```

- [ ] **Step 6: Typecheck**

Run: `cd frontend-vite && npx tsc --noEmit`
Expected: no errors (exit 0)

- [ ] **Step 7: Commit**

```bash
git add frontend-vite/src/components/TrimDialog.tsx
git commit -m "feat(trim-dialog): 静音裁剪 button — preview suggested end frame before confirm"
```

---

## Task 5: Playwright e2e — mocked silence detection

**Files:**
- Create: `tests/e2e/auto-silence-trim.spec.ts`

**Interfaces:**
- Consumes: the `detect-silence` endpoint contract from Task 2 (mocked, never hits real ffmpeg). Follows the existing patterns in `tests/e2e/subplan5-shot-review.spec.ts` and `tests/e2e/subplan7-new-features.spec.ts` for opening a project's shot review and the trim dialog.

> **Implementer note:** Before writing assertions, open `tests/e2e/subplan5-shot-review.spec.ts` and `playwright.config.ts` to copy this repo's exact bootstrapping (baseURL, how a project with a generated shot is seeded/navigated to, and how the Scissors/裁剪 button that sets `isTrimOpen` is reached in `ShotCard.tsx`). Reuse those selectors rather than inventing new ones. Mock **all** AI-triggering endpoints per project rules, not just `detect-silence`.

- [ ] **Step 1: Write the spec**

Create `tests/e2e/auto-silence-trim.spec.ts`:

```typescript
import { test, expect } from '@playwright/test'

// Mirror the project/shot bootstrap used by subplan5-shot-review.spec.ts.
// Replace openTrimDialogForFirstShot() body with the repo's existing helper/steps.

test.describe('Auto silence trim (suggest-only)', () => {
  test('静音裁剪 moves the slider to the suggested frame', async ({ page }) => {
    await page.route('**/api/projects/*/shots/*/detect-silence', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          has_silence: true,
          suggested_end_frame: 120,
          silence_start_time: 4.8,
          fps: 24,
          total_frames: 200,
          duration: 8.33,
        }),
      })
    })

    await openTrimDialogForFirstShot(page)

    await page.getByRole('button', { name: '静音裁剪' }).click()

    // Slider (input[type=range]) should now read the suggested frame.
    await expect(page.locator('input[type="range"]')).toHaveValue('120')
    // Frame readout reflects the suggestion.
    await expect(page.getByText('帧: 120 / 200')).toBeVisible()
    // Dialog stays open (suggest-only, not applied): 确认裁剪 still present.
    await expect(page.getByRole('button', { name: '确认裁剪' })).toBeVisible()
  })

  test('shows a notice when there is no trailing silence', async ({ page }) => {
    await page.route('**/api/projects/*/shots/*/detect-silence', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          has_silence: false,
          suggested_end_frame: null,
          silence_start_time: null,
          fps: 24,
          total_frames: 200,
          duration: 8.33,
        }),
      })
    })

    await openTrimDialogForFirstShot(page)
    await page.getByRole('button', { name: '静音裁剪' }).click()

    await expect(page.getByText('无尾部静音可裁剪')).toBeVisible()
  })
})

// TODO(implementer): copy the real bootstrap from subplan5-shot-review.spec.ts.
async function openTrimDialogForFirstShot(page: import('@playwright/test').Page) {
  // 1. Seed/navigate to a project whose first shot has a generated video
  //    (reuse the exact seeding + AI-endpoint mocks from subplan5).
  // 2. Click the Scissors/裁剪 button on the first ShotCard to open TrimDialog
  //    (the button that calls setIsTrimOpen(true)).
  // 3. Wait for the dialog title "裁剪视频" to be visible.
  await expect(page.getByText('裁剪视频')).toBeVisible()
}
```

- [ ] **Step 2: Run the spec**

Run: `npx playwright test tests/e2e/auto-silence-trim.spec.ts`
Expected: PASS once `openTrimDialogForFirstShot` is filled in with the repo's bootstrap. If the suite requires the dev stack, start it per project rules (`podman compose -f deploy/docker-compose.dev.yml up -d` + vite) before running.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/auto-silence-trim.spec.ts
git commit -m "test(e2e): auto silence trim — mocked detect-silence, slider + notice"
```

---

## Self-Review

**1. Spec coverage:**
- 仅尾部静音 / 复用 detect_speech_end → Task 1. ✓
- 按钮 + 动态检测，无新状态字段 → Task 4 (button, no DB field). ✓
- 落点 A：静音起点 +3 帧缓冲 → Task 1 `SILENCE_TAIL_PADDING_FRAMES = 3`. ✓
- suggest-only 端点（不写文件 / 不重置状态）→ Task 2 (read-only). ✓
- 边界：无静音→has_silence:false；<24 钳到 24；≥total→None → Task 1 tests cover all four + tiny-video clamp ordering. ✓
- 前端：滑块跳转、不关闭对话框、确认裁剪走现有 trim → Task 4 (slider-only) + Task 5 (dialog stays open). ✓
- 测试：service 单测 mock detect_speech_end/get_video_info；Playwright mock 端点 → Tasks 1 & 5. ✓
- 素材审计：零副作用 → enforced in Global Constraints + Task 2 docstring/implementation. ✓

**2. Placeholder scan:** No TBD/TODO in code steps. The single `TODO(implementer)` in Task 5 is an intentional, scoped pointer to copy the repo's existing e2e bootstrap (a real fixture that varies by repo state), not a code placeholder — its required behavior is fully specified in the surrounding note.

**3. Type consistency:** `suggested_end_frame`, `silence_start_time`, `has_silence`, `fps`, `total_frames`, `duration` identical across backend function (Task 1), endpoint (Task 2), API client type (Task 3), handler usage (Task 4), and mocked payloads (Task 5). Frame floor `24` consistent between backend `MIN_TRIM_FRAMES` and frontend `minFrames`. Padding `3` defined once in Task 1.
