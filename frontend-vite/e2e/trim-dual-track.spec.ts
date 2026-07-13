/**
 * TrimDialog dual-track cut line — REAL end-to-end flow.
 *
 * Per CLAUDE.md "E2E Tests — NEVER fake the data or flow under test": the
 * existing `waveform-trim.spec.ts` fakes `GET /api/projects/:id` via
 * `route.fulfill`, which is the forbidden fake-e2e pattern. This test instead
 * seeds a REAL, isolated project + completed shot directly into the real DB
 * (via `podman exec` into the running backend container — mirrors
 * `tests/e2e/nondestructive-real.spec.ts` / `tests/helpers/api.ts`'s
 * `execSeed` convention), copies a REAL already-generated `output_*.mp4` (no
 * model call, no billing), drives the real dual-track cut line in the browser,
 * and asserts the REAL post-trim `trim_frames` via a real `GET /api/projects/{id}`.
 */
import { test, expect } from '@playwright/test'
import { execSync } from 'node:child_process'

const BACKEND = 'http://localhost:8002'
const USER = 'e2e-dual'
const HEADERS = { 'X-User-Name': USER }

let pid = ''

test.beforeAll(async () => {
  // Real seed: new project (status=shot_review) + shot 1 (status=completed,
  // real video copied from an existing generated output_*.mp4, real
  // source_fps/source_frames read via ffprobe) — see
  // backend/tests/e2e_seed/seed_trim_dual_track.py for the full seed logic.
  const out = execSync(
    `podman exec video-maker-backend-dev uv run --project /app python /app/tests/e2e_seed/seed_trim_dual_track.py '{}'`,
    { encoding: 'utf8' }
  ).trim()
  const lines = out.split('\n')
  pid = lines[lines.length - 1].trim()
  expect(pid).toMatch(/^[0-9a-f-]{36}$/)
})

test.afterAll(async () => {
  if (!pid) return
  // Real teardown: DELETE cascades the DB row and removes the project's storage dir.
  await fetch(`${BACKEND}/api/projects/${pid}`, { method: 'DELETE', headers: HEADERS })
})

test('拖拽双轨裁剪线 → 确认裁剪 → 真实 trim_frames 落库变化', async ({ page }) => {
  await page.goto(`/projects/${pid}/shots`)
  await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 12_000 })

  const card = page.getByTestId('shot-card-1')
  await expect(card).toBeVisible({ timeout: 12_000 })
  await card.getByRole('button', { name: '裁剪' }).click()
  await expect(page.getByText('裁剪视频 — Shot #1')).toBeVisible({ timeout: 5_000 })

  // Dual-track timeline renders: video filmstrip + audio waveform + shared cut line.
  await expect(page.getByTestId('video-track')).toBeVisible({ timeout: 10_000 })
  await expect(page.getByTestId('audio-track')).toBeVisible({ timeout: 10_000 })
  const line = page.getByTestId('cut-line')
  await expect(line).toBeVisible()

  // Read the real source_frames via the real API to know what "60%" means,
  // and to assert against afterwards.
  const before = await (await fetch(`${BACKEND}/api/projects/${pid}`, { headers: HEADERS })).json()
  const shotBefore = before.shots.find((s: any) => s.shot_id === 1)
  expect(shotBefore.trim_frames).toBeNull()
  expect(shotBefore.source_frames).toBeGreaterThan(30)

  // Drag the cut line from the right edge (untrimmed = full length) to ~60%
  // of the audio track's width — a real pointer drag on the shared cut line.
  const track = await page.getByTestId('audio-track').boundingBox()
  const lineBox = await line.boundingBox()
  expect(track).not.toBeNull()
  expect(lineBox).not.toBeNull()

  await page.mouse.move(lineBox!.x + lineBox!.width / 2, lineBox!.y + lineBox!.height / 2)
  await page.mouse.down()
  const targetX = track!.x + track!.width * 0.6
  await page.mouse.move(targetX, lineBox!.y + lineBox!.height / 2, { steps: 10 })
  await page.mouse.up()

  // The frame counter should now show a cut (fewer frames than the source).
  await expect(page.getByText(/裁掉\s*\d+\s*帧/)).toBeVisible({ timeout: 5_000 })

  await page.getByRole('button', { name: '确认裁剪' }).click()
  // Dialog closes once the real /trim PUT completes.
  await expect(page.getByText('裁剪视频 — Shot #1')).not.toBeVisible({ timeout: 10_000 })

  // Real post-action state: GET the project again — the REAL trim must have
  // persisted to trim_frames, strictly less than the real source_frames.
  const after = await (await fetch(`${BACKEND}/api/projects/${pid}`, { headers: HEADERS })).json()
  const shotAfter = after.shots.find((s: any) => s.shot_id === 1)
  expect(shotAfter.trim_frames).not.toBeNull()
  expect(shotAfter.trim_frames).toBeGreaterThan(0)
  expect(shotAfter.trim_frames).toBeLessThan(shotAfter.source_frames)
})
