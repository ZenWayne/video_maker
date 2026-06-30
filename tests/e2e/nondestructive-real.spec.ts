/**
 * Non-destructive editing — REAL end-to-end flow (no faked data, no LLM, no seeding).
 *
 * Per CLAUDE.md "E2E Tests — NEVER fake the data or flow under test":
 *  - Uses an EXISTING already-generated project discovered via the real API
 *    (no DB seeding, no Python, no raw SQL — the frontend's own data).
 *  - Drives the real UI + real HTTP endpoints (/trim, /join-preview), and asserts
 *    on the real resulting state (API + the real player/preview).
 *  - Restores every shot it touches in afterAll, so the project is left as found.
 *
 * Catches the bugs a mocked e2e missed: trim must reach the player, and the
 * continuity preview must stitch the TRIMMED clips (not the full sources).
 *
 * Note: VC playback / A-B toggle is NOT exercised here — producing a voice-cloned
 * track requires a CosyVoice (model) run, which e2e must not trigger. That path is
 * covered by the backend integration tests (test_vc_nondestructive.py).
 */
import { test, expect, request } from '@playwright/test'
import { execSync } from 'node:child_process'

const BACKEND = 'http://localhost:8002'
const HEADERS = { 'X-User-Name': 'e2e-nondestructive' }

let pid = ''
let shotA = 0
let shotB = 0
let origA: number | null = null
let origB: number | null = null

test.beforeAll(async () => {
  // Discover an existing project with >=2 completed shots whose video files are
  // actually present on disk (video-info 200) — the shots grid renders for any
  // status except the script/generating phases.
  const SKIP_STATUS = new Set(['draft', 'scripting', 'script_review', 'shot_generating'])
  const ctx = await request.newContext()
  const list = await (await ctx.get(`${BACKEND}/api/projects`, { headers: HEADERS })).json()
  for (const p of list.items || []) {
    const d = await (await ctx.get(`${BACKEND}/api/projects/${p.id}`, { headers: HEADERS })).json()
    if (SKIP_STATUS.has(d.status)) continue
    const shots = (d.shots || [])
      .filter((s: any) => s.status === 'completed' && s.video_path)
      .sort((a: any, b: any) => a.shot_id - b.shot_id)
    if (shots.length < 2) continue
    // verify the first two shots' video files are usable (exist + decodable)
    const usable = []
    for (const s of shots) {
      const vi = await ctx.get(`${BACKEND}/api/projects/${d.id}/shots/${s.shot_id}/video-info`, { headers: HEADERS })
      if (vi.ok() && (await vi.json()).total_frames > 30) usable.push(s)
      if (usable.length === 2) break
    }
    if (usable.length === 2) {
      pid = d.id
      shotA = usable[0].shot_id; origA = usable[0].trim_frames ?? null
      shotB = usable[1].shot_id; origB = usable[1].trim_frames ?? null
      break
    }
  }
  await ctx.dispose()
})

test.afterAll(async () => {
  if (!pid) return
  const ctx = await request.newContext()
  const restore = async (sid: number, orig: number | null) => {
    if (orig == null) {
      await ctx.post(`${BACKEND}/api/projects/${pid}/shots/${sid}/restore-trim`, { headers: HEADERS }).catch(() => {})
    } else {
      await ctx.post(`${BACKEND}/api/projects/${pid}/shots/${sid}/trim`, { headers: HEADERS, data: { end_frame: orig } }).catch(() => {})
    }
  }
  await restore(shotA, origA)
  await restore(shotB, origB)
  await ctx.dispose()
})

async function setRange(locator: import('@playwright/test').Locator, value: number) {
  await locator.evaluate((el: HTMLInputElement, v: number) => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!
    setter.call(el, String(v))
    el.dispatchEvent(new Event('input', { bubbles: true }))
  }, value)
}

test('real trim: UI trim persists AND the dialog reopens at the trimmed length', async ({ page }) => {
  test.skip(!pid, 'no shot_review project with >=2 completed shots available')

  await page.goto(`/projects/${pid}/shots`)
  const card = page.locator(`[data-testid="shot-card-${shotA}"]`)
  await expect(card).toBeVisible({ timeout: 12_000 })

  // open the REAL trim dialog
  await card.getByRole('button', { name: '裁剪' }).click()
  const range = page.locator('input[type="range"]')
  await expect(range).toBeVisible({ timeout: 8_000 })
  // wait for getVideoInfo to populate the slider bounds (max starts at 0 while loading)
  await expect.poll(async () => Number(await range.getAttribute('max')), { timeout: 15_000 }).toBeGreaterThan(30)
  const total = Number(await range.getAttribute('max'))
  const target = Math.floor(total * 0.6)

  // set slider + confirm — hits the REAL POST /trim
  await setRange(range, target)
  await expect.poll(async () => Number(await range.inputValue())).toBe(target)
  await page.getByRole('button', { name: '确认裁剪' }).click()
  await expect(page.locator('input[type="range"]')).toHaveCount(0)

  // (a) real API reflects the trim
  const ctx = await request.newContext()
  const proj = await (await ctx.get(`${BACKEND}/api/projects/${pid}`, { headers: HEADERS })).json()
  await ctx.dispose()
  const s = proj.shots.find((x: any) => x.shot_id === shotA)
  expect(s.trim_frames).toBe(target)
  expect(s.trim_end_sec).toBeGreaterThan(0)

  // (b) THE BUG: reopen the dialog — the slider must sit at the trimmed length
  //     (under the old code the trim never reached the shot → it stayed `total`).
  await card.getByRole('button', { name: '裁剪' }).click()
  const range2 = page.locator('input[type="range"]')
  await expect(range2).toBeVisible({ timeout: 8_000 })
  await expect.poll(async () => Number(await range2.inputValue())).toBe(target)
  expect(Number(await range2.inputValue())).toBeLessThan(total)
})

test('real continuity preview: stitches TRIMMED clips, not the full source', async ({ page }) => {
  test.skip(!pid, 'no shot_review project with >=2 completed shots available')
  test.setTimeout(90_000) // real two-clip trim + concat merge can be slow

  // trim both shots to 40 frames via the REAL /trim endpoint → the stitched
  // preview must have ~80 video frames (40+40), not the full sources (~hundreds).
  const TRIM = 40
  const ctx = await request.newContext()
  await ctx.post(`${BACKEND}/api/projects/${pid}/shots/${shotA}/trim`, { headers: HEADERS, data: { end_frame: TRIM } })
  await ctx.post(`${BACKEND}/api/projects/${pid}/shots/${shotB}/trim`, { headers: HEADERS, data: { end_frame: TRIM } })
  await ctx.dispose()

  await page.goto(`/projects/${pid}/shots`)
  await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 12_000 })

  // select both shots and build the continuity preview via the REAL endpoint
  await page.getByTestId(`shot-select-${shotA}`).click()
  await page.getByTestId(`shot-select-${shotB}`).click()
  await page.getByTestId('join-preview-button').click()

  // the stitched preview's duration ≈ sum of TRIMMED durations (not the full sources)
  const preview = page.locator('video[src*="join_preview"]')
  await expect(preview).toBeVisible({ timeout: 30_000 })
  // ffprobe the produced preview directly over HTTP (local ffprobe reads URLs).
  // Count VIDEO frames (container duration is audio-padded and unreliable).
  const src = await preview.getAttribute('src')
  const url = src!.startsWith('http') ? src! : `${BACKEND}${src}`
  const frames = Number(execSync(
    `ffprobe -v error -count_frames -select_streams v:0 ` +
    `-show_entries stream=nb_read_frames -of csv=p=0 "${url}"`,
    { encoding: 'utf8' }).trim())
  // ~80 trimmed frames (40+40), allow concat rounding; the bug stitched the
  // full sources → many hundreds of frames.
  expect(frames).toBeGreaterThan(2 * TRIM - 8)
  expect(frames).toBeLessThan(2 * TRIM + 8)
})
