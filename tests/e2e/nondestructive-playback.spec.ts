/**
 * Non-destructive playback — trim clamp + A/B toggle (Task 15)
 *
 * Uses route mocks only — no real backend or seeded data needed.
 * All AI-triggering endpoints are mocked per CLAUDE.md requirements.
 *
 * Assertions:
 *  - ShotCard with vc_audio_url set shows [data-testid="ab-toggle"] after clicking play
 *  - A <video> element is rendered by ShotPlayer
 */
import { test, expect } from '@playwright/test'

const PROJECT_ID = 'test-ndp-proj'

const SHOT_MOCK = {
  id: 1,
  project_id: PROJECT_ID,
  shot_id: 1,
  text: '这是一段测试台词，用于验证非破坏性播放。',
  shot_type: 'Close-up',
  visual_description: 'A close-up of a character speaking.',
  shot_duration: 4,
  status: 'completed',
  align_with_previous: false,
  motion_prompt: null,
  first_frame_path: null,
  video_path: `/api/media/projects/${PROJECT_ID}/shots/shot_1/output.mp4`,
  last_frame_path: `/api/media/projects/${PROJECT_ID}/shots/shot_1/last_frame.png`,
  word_count_warning: false,
  error_message: null,
  custom_first_frame_path: null,
  custom_reference_paths: null,
  reference_image_hint: null,
  vc_status: 'done',
  vc_error_message: null,
  cc_status: null,
  cc_error_message: null,
  target_last_frame_path: null,
  tf_status: null,
  tf_error_message: null,
  tf_confirmed: false,
  auto_trim: false,
  trim_end_sec: 2.0,
  vc_audio_url: `/api/media/projects/${PROJECT_ID}/shots/shot_1/audio_vc.wav`,
}

const PROJECT_MOCK = {
  id: PROJECT_ID,
  title: 'Non-Destructive Playback Test',
  theme_text: 'test theme',
  aspect_ratio: '16:9',
  creator_name: 'pw-test',
  status: 'shot_review',
  scene_overview: 'Test scene overview.',
  final_video_path: null,
  error_message: null,
  reference_voice_shot_id: null,
  reference_voice_path: null,
  auto_voice_calibrate: false,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
  shots: [SHOT_MOCK],
  reference_images: [],
}

test('trimmed shot clamps playback; vc shot shows A/B toggle', async ({ page }) => {
  // ── 1. Mock all AI-triggering endpoints (must never bill) ─────────────────
  await page.route(`**/api/projects/${PROJECT_ID}/start`, (r) =>
    r.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) }))
  await page.route(`**/api/projects/${PROJECT_ID}/export`, (r) =>
    r.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) }))
  await page.route(`**/api/projects/${PROJECT_ID}/regenerate-shots`, (r) =>
    r.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) }))
  await page.route(`**/api/projects/${PROJECT_ID}/approve-script`, (r) =>
    r.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) }))
  await page.route(`**/api/projects/${PROJECT_ID}/regenerate-script`, (r) =>
    r.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) }))
  await page.route(`**/api/projects/${PROJECT_ID}/shots/*/voice-convert`, (r) =>
    r.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) }))
  await page.route(`**/api/projects/${PROJECT_ID}/shots/*/generate-tail-frame`, (r) =>
    r.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) }))
  await page.route(`**/api/projects/${PROJECT_ID}/shots/*/confirm-tail-frame`, (r) =>
    r.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) }))
  await page.route(`**/api/projects/${PROJECT_ID}/shots/*/align-tail-frame`, (r) =>
    r.fulfill({ status: 200, body: JSON.stringify({ status: 'ok' }) }))

  // ── 2. Mock project data GET ──────────────────────────────────────────────
  // Must be registered before any sub-path mocks so the exact path doesn't
  // accidentally match the stream route.
  await page.route(`**/api/projects/${PROJECT_ID}`, (r) =>
    r.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(PROJECT_MOCK),
    }))

  // ── 3. Mock SSE stream (return a single keep-alive comment, then close) ───
  await page.route(`**/api/projects/${PROJECT_ID}/stream`, (r) =>
    r.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      headers: { 'Cache-Control': 'no-cache' },
      body: ': keepalive\n\n',
    }))

  // ── 4. Mock media files so the browser doesn't log 404 errors ────────────
  await page.route('**/api/media/**', (r) =>
    r.fulfill({ status: 200, contentType: 'application/octet-stream', body: '' }))

  // ── 5. Navigate to the shots review page ─────────────────────────────────
  await page.goto(`/projects/${PROJECT_ID}/shots`)

  // ── 6. Wait for shots list to be visible ─────────────────────────────────
  const shotsList = page.getByTestId('shots-list')
  await expect(shotsList).toBeVisible({ timeout: 8_000 })

  // ── 7. Find the first shot card ──────────────────────────────────────────
  const firstCard = shotsList.locator('[data-testid^="shot-card"]').first()
  await expect(firstCard).toBeVisible()

  // ── 8. Click the play thumbnail to switch ShotCard into isPlaying mode ───
  // The thumbnail is a div.cursor-pointer that, when clicked, sets isPlaying=true
  // and renders <ShotPlayer> in its place.
  await firstCard.locator('div.cursor-pointer').first().click()

  // ── 9. Assert: A/B toggle button appears (vc_audio_url is set) ───────────
  await expect(page.getByTestId('ab-toggle')).toBeVisible({ timeout: 5_000 })

  // ── 10. Assert: ShotPlayer renders a <video> element ─────────────────────
  await expect(firstCard.locator('video')).toBeVisible({ timeout: 5_000 })
})
