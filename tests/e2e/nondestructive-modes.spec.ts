/**
 * Non-destructive playback — mode matrix + audio-error fallback (authored by hand).
 *
 * Covers gaps left by the other two specs:
 *  1) Non-VC shot (vc_audio_url=null) → ShotPlayer renders a plain <video>:
 *     NO <audio>, NO ab-toggle, NO error message.
 *  2) VC shot whose audio FAILS to load → onError fallback:
 *     video becomes UNMUTED and the "配音音轨加载失败" message is shown.
 *
 * Route-mocked only (no real backend / no billing), per CLAUDE.md.
 */
import { test, expect, Page } from '@playwright/test'

function baseShot(projectId: string) {
  return {
    id: 1, project_id: projectId, shot_id: 1,
    text: '模式矩阵测试台词。', shot_type: 'Close-up',
    visual_description: 'A close-up.', shot_duration: 4, status: 'completed',
    align_with_previous: false, motion_prompt: null, first_frame_path: null,
    video_path: `/api/media/projects/${projectId}/shots/shot_1/output_1_a.mp4`,
    last_frame_path: `/api/media/projects/${projectId}/shots/shot_1/last_frame_1_a.png`,
    word_count_warning: false, error_message: null,
    custom_first_frame_path: null, custom_reference_paths: null, reference_image_hint: null,
    vc_status: null, vc_error_message: null, cc_status: null, cc_error_message: null,
    target_last_frame_path: null, tf_status: null, tf_error_message: null, tf_confirmed: false,
    auto_trim: false,
    trim_frames: null, source_fps: null, source_frames: null, trim_end_sec: null, vc_audio_url: null,
  }
}

async function setupMocks(page: Page, projectId: string, shot: Record<string, unknown>) {
  for (const ep of ['start', 'export', 'regenerate-shots', 'approve-script', 'regenerate-script']) {
    await page.route(`**/api/projects/${projectId}/${ep}`, (r) =>
      r.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) }))
  }
  for (const ep of ['voice-convert', 'generate-tail-frame', 'confirm-tail-frame', 'align-tail-frame']) {
    await page.route(`**/api/projects/${projectId}/shots/*/${ep}`, (r) =>
      r.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) }))
  }
  await page.route(`**/api/projects/${projectId}`, (r) =>
    r.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        id: projectId, title: 'Modes Test', theme_text: 't', aspect_ratio: '16:9',
        creator_name: 'pw', status: 'shot_review', scene_overview: 's',
        final_video_path: null, error_message: null,
        reference_voice_shot_id: null, reference_voice_path: null, auto_voice_calibrate: false,
        created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z',
        shots: [shot], reference_images: [],
      }),
    }))
  await page.route(`**/api/projects/${projectId}/stream`, (r) =>
    r.fulfill({ status: 200, contentType: 'text/event-stream',
      headers: { 'Cache-Control': 'no-cache' }, body: ': keepalive\n\n' }))
  // Empty body for all media — for the VC test this makes the <audio> fail to load.
  await page.route('**/api/media/**', (r) =>
    r.fulfill({ status: 200, contentType: 'application/octet-stream', body: '' }))
}

async function play(page: Page) {
  const list = page.getByTestId('shots-list')
  await expect(list).toBeVisible({ timeout: 8_000 })
  const card = list.locator('[data-testid^="shot-card"]').first()
  await expect(card).toBeVisible()
  await card.locator('div.cursor-pointer').first().click()
  return card
}

test('non-VC shot: plain video, no audio element, no A/B toggle', async ({ page }) => {
  const PID = 'test-ndp-plain'
  await setupMocks(page, PID, baseShot(PID))
  await page.goto(`/projects/${PID}/shots`)
  const card = await play(page)

  await expect(card.locator('video')).toBeVisible({ timeout: 5_000 })
  await expect(card.locator('audio')).toHaveCount(0)
  await expect(page.getByTestId('ab-toggle')).toHaveCount(0)
  await expect(page.getByTestId('audio-error-msg')).toHaveCount(0)
})

test('VC shot with failing audio: falls back to source audio + shows message', async ({ page }) => {
  const PID = 'test-ndp-audiofail'
  const shot = {
    ...baseShot(PID),
    vc_status: 'done',
    trim_frames: 60, source_fps: 30, source_frames: 120, trim_end_sec: 2.0,
    vc_audio_url: `/api/media/projects/${PID}/shots/shot_1/audio_vc_1_a.wav`, // empty body → load error
  }
  await setupMocks(page, PID, shot)
  await page.goto(`/projects/${PID}/shots`)
  const card = await play(page)

  // onError fallback: error message appears and the video is unmuted (source audio)
  await expect(page.getByTestId('audio-error-msg')).toBeVisible({ timeout: 5_000 })
  await expect.poll(async () =>
    card.locator('video').evaluate((el: HTMLVideoElement) => el.muted)).toBe(false)
})
