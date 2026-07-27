/**
 * Signed COS video URL expiry — REAL end-to-end flow.
 *
 * Per CLAUDE.md "E2E Tests — NEVER fake the data or flow under test": this
 * seeds a REAL, isolated project + completed shot directly into the real DB
 * (via `podman exec` into the running backend container — mirrors
 * trim-dual-track.spec.ts's convention), reusing a REAL already-uploaded shot
 * video object already sitting in the shared COS bucket (server-side
 * COS-copied into the new shot's key — no model call, no billing), drives
 * the real browser against the real backend, and asserts the REAL
 * `GET /api/projects/{id}` refetch plus REAL video recovery.
 *
 * The ONLY thing faked here is the network condition itself: the FIRST
 * request to the (real, COS-hosted) video URL is short-circuited to a 403 to
 * simulate the signed URL's TTL having expired — every request after that
 * passes straight through to the real, signed COS URL. This is stubbing a
 * network failure, not the data/flow under test (see CLAUDE.md's explicit
 * allowance for this exact scenario).
 */
import { test, expect } from '@playwright/test'
import { execSync } from 'node:child_process'

const BACKEND = 'http://localhost:8002'
const USER = 'e2e-expiry'
const HEADERS = { 'X-User-Name': USER }

let pid = ''

test.beforeAll(async () => {
  // Real seed: new isolated project (status=shot_review) + shot 1
  // (status=completed, video_path = a REAL COS object reused via server-side
  // copy from the shared bucket) — see
  // backend/tests/e2e_seed/seed_signed_url_expiry.py for the full seed logic.
  const out = execSync(
    `podman exec video-maker-backend-dev sh -c '` +
      `export COS_SECRET_ID=$(cat /run/secrets/cos_secret_id) && ` +
      `export COS_SECRET_KEY=$(cat /run/secrets/cos_secret_key) && ` +
      `uv run --project /app python /app/tests/e2e_seed/seed_signed_url_expiry.py "{}"'`,
    { encoding: 'utf8' }
  ).trim()
  const lines = out.split('\n')
  pid = lines[lines.length - 1].trim()
  expect(pid).toMatch(/^[0-9a-f-]{36}$/)
})

test.afterAll(async () => {
  if (!pid) return
  // Real teardown: DELETE cascades the DB row and removes the project's
  // storage (COS) objects.
  await fetch(`${BACKEND}/api/projects/${pid}`, { method: 'DELETE', headers: HEADERS })
})

test('视频 URL 过期时自动重拉项目并恢复播放，只重试一次', async ({ page }) => {
  await page.goto(`/projects/${pid}/shots`)
  await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 12_000 })

  const card = page.getByTestId('shot-card-1')
  await expect(card).toBeVisible({ timeout: 12_000 })

  const video = card.locator('video').first()
  await expect(video).toBeVisible({ timeout: 10_000 })

  // Real src is a real, currently-valid, signed COS URL — confirms we're not
  // looking at some placeholder before the network games start.
  const initialSrc = await video.getAttribute('src')
  expect(initialSrc).toMatch(/\.myqcloud\.com\/projects\//)

  // Track every real GET /api/projects/{pid} the frontend fires from here on
  // (the mount-time GET has already happened by now).
  let refetchCount = 0
  page.on('request', (r) => {
    if (r.method() === 'GET' && r.url().includes(`/api/projects/${pid}`) && !r.url().includes('/events')) {
      refetchCount += 1
    }
  })

  // Signed COS URLs embed a whole-second signing timestamp
  // (q-sign-time=<start>;<end>). A retry fired in the very same wall-clock
  // second as the original page load would (correctly, and harmlessly)
  // re-sign to a BYTE-IDENTICAL url — that's not a bug, it's just this test
  // triggering a "2-hour-later" expiry mere milliseconds after the original
  // load. Wait past that second boundary so the retry's re-signed URL is
  // guaranteed to actually differ, same as a real 2-hours-later expiry would.
  await page.waitForTimeout(1300)

  // Simulate the signed URL's TTL having expired: the FIRST request to the
  // (real) video object gets a 403; every request after that is a REAL,
  // unmolested request against the REAL signed COS URL.
  let mockedOnce = false
  await page.route('**/*.mp4*', async (route) => {
    if (!mockedOnce) {
      mockedOnce = true
      await route.fulfill({ status: 403, contentType: 'application/xml', body: 'AccessDenied' })
    } else {
      await route.continue()
    }
  })

  // preload="none" — force the load so the network request actually fires.
  await video.evaluate((el: HTMLVideoElement) => el.load())

  // The onError handler must fire exactly one real refetch of the project.
  await expect.poll(() => refetchCount, { timeout: 10_000 }).toBe(1)

  // ...and the video's src must actually recover to a freshly-signed URL
  // (error must clear too — no longer stuck in the 403's error state).
  await expect
    .poll(() => video.evaluate((el: HTMLVideoElement) => el.error === null), { timeout: 10_000 })
    .toBe(true)
  const recoveredSrc = await video.getAttribute('src')
  expect(recoveredSrc).not.toBe(initialSrc)

  // Prove the fresh URL is genuinely playable, not just "didn't error because
  // preload=none never re-fetched": force a real load against it (unmocked —
  // mockedOnce is already true) and wait for real metadata to actually
  // decode.
  await video.evaluate((el: HTMLVideoElement) => el.load())
  await expect
    .poll(
      () => video.evaluate((el: HTMLVideoElement) => el.readyState >= 1 /* HAVE_METADATA */ && el.error === null),
      { timeout: 10_000 }
    )
    .toBe(true) // real decode succeeded against the real, unmocked, freshly-signed COS URL

  // The "retry only once per URL" behavior itself (no infinite loop on a
  // genuinely broken clip, but a fresh URL gets its own retry budget) is
  // covered precisely at the component level in
  // src/hooks/__tests__/useVideoErrorRetry.test.ts and
  // src/components/__tests__/ShotPlayer.errorRetry.test.tsx — asserting it
  // here too would just be re-testing timing, not the real wiring.
})
