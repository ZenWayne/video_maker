/**
 * Non-destructive editing — REAL end-to-end flow (no faked data, no LLM).
 *
 * Per CLAUDE.md "E2E Tests — NEVER fake the data or flow under test":
 *  - Project created via the REAL API.
 *  - Shots seeded by REUSING an existing already-generated video (copied into a
 *    fresh isolated project) + a real audio track extracted with ffmpeg — so the
 *    only thing skipped is the billed generation; everything else is real.
 *  - The trim/playback flow exercises the REAL UI → REAL /trim endpoint → REAL DB
 *    → REAL serialization → REAL ShotPlayer. Assertions read the real API state
 *    and the real UI, never injected mock values.
 *
 * This catches the bug a mocked e2e missed: after trimming, the player must
 * actually receive the trim (trim_end_sec) — i.e. the dialog reopens at the
 * trimmed length, not the full source length.
 */
import { test, expect, request } from '@playwright/test'
import { execSync } from 'node:child_process'

const BACKEND = 'http://localhost:8002'
const HEADERS = { 'X-User-Name': 'e2e-nondestructive' }
const CTR = 'video-maker-backend-dev'

// Seeds an isolated project's shots by REUSING existing generated videos in the
// shared storage volume (no LLM). Runs inside the backend container against the
// real DB + storage. pid passed as argv; python code via stdin.
const SEED_PY = `
import sqlite3, shutil, os, time, subprocess, uuid, sys, datetime
pid = sys.argv[1]
now = datetime.datetime.utcnow().isoformat()
DB = '/app/data/dev.db'
STORE = '/app/storage'
# existing already-generated videos to reuse (must exist in the shared volume)
SRC = sorted(__import__('glob').glob(f'{STORE}/projects/*/shots/shot_*/output_*.mp4'))
src = [p for p in SRC if 'output_pre_vc' not in p and 'output_original' not in p]
assert len(src) >= 2, f'need >=2 existing generated videos, found {len(src)}'
db = sqlite3.connect(DB)
db.execute("UPDATE projects SET status='shot_review' WHERE id=?", (pid,))
def seed(sid, source, with_vc):
    d = f"{STORE}/projects/{pid}/shots/shot_{sid}"
    os.makedirs(d, exist_ok=True)
    vid = f"{d}/output_{int(time.time())}_{uuid.uuid4().hex[:8]}.mp4"
    shutil.copy(source, vid)
    # extract a real frame so the play thumbnail has size (clickable)
    frame = f"{d}/last_frame_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
    subprocess.run(["ffmpeg","-y","-i",vid,"-vframes","1",frame], check=True, capture_output=True)
    vc = None
    if with_vc:
        vc = f"{d}/audio_vc_{int(time.time())}_{uuid.uuid4().hex[:8]}.wav"
        subprocess.run(["ffmpeg","-y","-i",source,"-vn","-acodec","pcm_s16le",vc], check=True, capture_output=True)
    db.execute(
        "INSERT INTO shots (project_id,shot_id,text,shot_type,visual_description,shot_duration,status,align_with_previous,use_prev_last_frame,auto_trim,word_count_warning,video_path,first_frame_path,last_frame_path,vc_status,vc_audio_path,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, sid, f"e2e shot {sid}", "Close-up", "desc", 6, "completed", 0, 1, 1, 0, vid,
         frame, frame, ("done" if with_vc else None), vc, now, now))
seed(1, src[0], False)   # trim target
seed(2, src[1], True)    # VC playback target
db.commit()
print("seeded", pid)
`

let pid = ''

test.beforeAll(async () => {
  // 1) create the project via the REAL API
  const ctx = await request.newContext()
  const r = await ctx.post(`${BACKEND}/api/projects`, {
    headers: HEADERS,
    data: { title: 'e2e non-destructive (real)', theme_text: 'e2e real flow' },
  })
  expect(r.status()).toBe(201)
  pid = (await r.json()).id
  await ctx.dispose()

  // 2) seed two completed shots by reusing real generated assets (no LLM)
  const out = execSync(`podman exec -i ${CTR} python3 - ${pid}`, { input: SEED_PY, encoding: 'utf8' })
  expect(out).toContain('seeded')
})

test.afterAll(async () => {
  if (!pid) return
  const ctx = await request.newContext()
  await ctx.delete(`${BACKEND}/api/projects/${pid}`, { headers: HEADERS }).catch(() => {})
  await ctx.dispose()
})

async function setRange(locator: import('@playwright/test').Locator, value: number) {
  await locator.evaluate((el: HTMLInputElement, v: number) => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!
    setter.call(el, String(v))
    el.dispatchEvent(new Event('input', { bubbles: true }))
  }, value)
}

test('real trim: UI trim persists to DB AND the player/dialog reflect it (trim_end_sec propagated)', async ({ page }) => {
  await page.goto(`/projects/${pid}/shots`)
  const card = page.locator('[data-testid="shot-card-1"]')
  await expect(card).toBeVisible({ timeout: 12_000 })

  // open the REAL trim dialog
  await card.getByRole('button', { name: '裁剪' }).click()
  const range = page.locator('input[type="range"]')
  await expect(range).toBeVisible({ timeout: 8_000 })
  const total = Number(await range.getAttribute('max'))
  expect(total).toBeGreaterThan(30)
  const target = Math.floor(total * 0.6)

  // set the slider and confirm — hits the REAL POST /trim
  await setRange(range, target)
  await expect.poll(async () => Number(await range.inputValue())).toBe(target)
  await page.getByRole('button', { name: '确认裁剪' }).click()
  await expect(page.locator('input[type="range"]')).toHaveCount(0) // dialog closed

  // (a) REAL API state reflects the trim (endpoint + serialization + schema)
  const ctx = await request.newContext()
  const proj = await (await ctx.get(`${BACKEND}/api/projects/${pid}`, { headers: HEADERS })).json()
  await ctx.dispose()
  const shot1 = proj.shots.find((s: any) => s.shot_id === 1)
  expect(shot1.trim_frames).toBe(target)
  expect(shot1.trim_end_sec).toBeGreaterThan(0)
  expect(shot1.video_path).toContain('/api/media/') // still the immutable source, served

  // (b) THE BUG: the frontend must have received the trim. Reopen the dialog —
  //     with the trim propagated, the slider sits at the trimmed length, not full.
  //     (Under the old code, trim_end_sec/trim_frames never reached the shot, so
  //     this would be `total`.)
  await card.getByRole('button', { name: '裁剪' }).click()
  const range2 = page.locator('input[type="range"]')
  await expect(range2).toBeVisible({ timeout: 8_000 })
  await expect.poll(async () => Number(await range2.inputValue())).toBe(target)
  expect(Number(await range2.inputValue())).toBeLessThan(total)
})

test('real VC shot: composites video + vc audio with working A/B toggle', async ({ page }) => {
  await page.goto(`/projects/${pid}/shots`)
  const card = page.locator('[data-testid="shot-card-2"]')
  await expect(card).toBeVisible({ timeout: 12_000 })

  // enter playing mode (renders ShotPlayer) — click the play-thumbnail container
  // (the div carrying onClick; the Play-icon overlay sits above the <img>)
  await card.locator('div.cursor-pointer:has(img[alt="Shot 2"])').click()

  const toggle = page.getByTestId('ab-toggle')
  await expect(toggle).toBeVisible({ timeout: 6_000 })
  const video = card.locator('video')
  const audio = card.locator('audio')
  await expect(video).toBeVisible()
  await expect(audio).toHaveCount(1)

  // default = vc track: video muted, vc audio audible (real files, real serialization)
  expect(await video.evaluate((el: HTMLVideoElement) => el.muted)).toBe(true)
  // toggle → source audio: video unmuted
  await toggle.click()
  await expect.poll(async () => video.evaluate((el: HTMLVideoElement) => el.muted)).toBe(false)
})
