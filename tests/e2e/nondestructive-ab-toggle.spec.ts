/**
 * Non-destructive playback — A/B audio toggle BEHAVIOR (authored by hand).
 *
 * Goes beyond visibility: asserts the actual compositing/mute state.
 *  - A VC shot composites as <video muted> (picture) + <audio> (vc track).
 *  - Clicking [data-testid="ab-toggle"] flips to source audio:
 *      video becomes UNMUTED, the vc <audio> becomes muted.
 *  - The <audio> is wired to the vc_audio_url; the <video> to the source.
 *
 * Route-mocked only (no real backend / no billing), per CLAUDE.md.
 */
import { test, expect } from '@playwright/test'

const PROJECT_ID = 'test-ndp-abtoggle'

/** A minimal valid PCM WAV (0.1s silence) so the <audio> element loads
 *  successfully and does NOT trip the onError → source-audio fallback. */
function tinyWav(): Buffer {
  const sampleRate = 8000
  const dataSize = 800 // 0.1s, 8-bit mono
  const buf = Buffer.alloc(44 + dataSize)
  buf.write('RIFF', 0); buf.writeUInt32LE(36 + dataSize, 4); buf.write('WAVE', 8)
  buf.write('fmt ', 12); buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20)
  buf.writeUInt16LE(1, 22); buf.writeUInt32LE(sampleRate, 24); buf.writeUInt32LE(sampleRate, 28)
  buf.writeUInt16LE(1, 32); buf.writeUInt16LE(8, 34)
  buf.write('data', 36); buf.writeUInt32LE(dataSize, 40)
  buf.fill(128, 44) // 8-bit unsigned silence
  return buf
}

const SHOT_MOCK = {
  id: 1,
  project_id: PROJECT_ID,
  shot_id: 1,
  text: 'A/B 切换行为测试台词。',
  shot_type: 'Close-up',
  visual_description: 'A close-up of a character speaking.',
  shot_duration: 4,
  status: 'completed',
  align_with_previous: false,
  motion_prompt: null,
  first_frame_path: null,
  video_path: `/api/media/projects/${PROJECT_ID}/shots/shot_1/output_1_a.mp4`,
  last_frame_path: `/api/media/projects/${PROJECT_ID}/shots/shot_1/last_frame_1_a.png`,
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
  trim_frames: 60,
  source_fps: 30,
  source_frames: 120,
  trim_end_sec: 2.0,
  vc_audio_url: `/api/media/projects/${PROJECT_ID}/shots/shot_1/audio_vc_1_a.wav`,
}

const PROJECT_MOCK = {
  id: PROJECT_ID,
  title: 'A/B Toggle Behavior Test',
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

test('A/B toggle flips video between vc track (muted) and source audio (unmuted)', async ({ page }) => {
  // Mock AI-triggering endpoints (must never bill)
  for (const ep of ['start', 'export', 'regenerate-shots', 'approve-script', 'regenerate-script']) {
    await page.route(`**/api/projects/${PROJECT_ID}/${ep}`, (r) =>
      r.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) }))
  }
  for (const ep of ['voice-convert', 'generate-tail-frame', 'confirm-tail-frame', 'align-tail-frame']) {
    await page.route(`**/api/projects/${PROJECT_ID}/shots/*/${ep}`, (r) =>
      r.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) }))
  }

  // Project data + SSE + media
  await page.route(`**/api/projects/${PROJECT_ID}`, (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PROJECT_MOCK) }))
  await page.route(`**/api/projects/${PROJECT_ID}/stream`, (r) =>
    r.fulfill({ status: 200, contentType: 'text/event-stream',
      headers: { 'Cache-Control': 'no-cache' }, body: ': keepalive\n\n' }))
  await page.route('**/api/media/**', (r) =>
    r.fulfill({ status: 200, contentType: 'application/octet-stream', body: '' }))
  // Serve a VALID wav for the vc track so <audio> loads (no onError fallback).
  // Registered after the generic media mock so it takes precedence.
  await page.route('**/audio_vc_1_a.wav', (r) =>
    r.fulfill({ status: 200, contentType: 'audio/wav', body: tinyWav() }))

  await page.goto(`/projects/${PROJECT_ID}/shots`)

  const shotsList = page.getByTestId('shots-list')
  await expect(shotsList).toBeVisible({ timeout: 8_000 })
  const firstCard = shotsList.locator('[data-testid^="shot-card"]').first()
  await expect(firstCard).toBeVisible()

  // Enter playing mode (renders ShotPlayer in place of the thumbnail)
  await firstCard.locator('div.cursor-pointer').first().click()

  const toggle = page.getByTestId('ab-toggle')
  await expect(toggle).toBeVisible({ timeout: 5_000 })

  const video = firstCard.locator('video')
  const audio = firstCard.locator('audio')
  await expect(video).toBeVisible({ timeout: 5_000 })

  // Wiring: video → source, audio → vc track
  await expect(video).toHaveAttribute('src', /output_1_a\.mp4/)
  await expect(audio).toHaveAttribute('src', /audio_vc_1_a\.wav/)

  // Default (vc track): video muted, vc audio audible
  expect(await video.evaluate((el: HTMLVideoElement) => el.muted)).toBe(true)
  expect(await audio.evaluate((el: HTMLAudioElement) => el.muted)).toBe(false)

  // Toggle → source audio: video unmuted, vc audio muted
  await toggle.click()
  await expect.poll(async () => video.evaluate((el: HTMLVideoElement) => el.muted)).toBe(false)
  expect(await audio.evaluate((el: HTMLAudioElement) => el.muted)).toBe(true)

  // Toggle back → vc track again
  await toggle.click()
  await expect.poll(async () => video.evaluate((el: HTMLVideoElement) => el.muted)).toBe(true)
})
