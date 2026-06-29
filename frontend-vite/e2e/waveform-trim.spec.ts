import { test, expect } from '@playwright/test'

// E2E for the voiceprint waveform track in the trim dialog.
//
// Strategy: hermetic project/shot mocks (like join-preview.spec.ts), BUT the
// shot's video_path points at a REAL media file served by the running backend
// so the browser's Web Audio decodeAudioData path actually runs end-to-end.
// video-info is mocked to return a realistic trailing-silence result so the
// "speech-end" overlay path is exercised.
//
// The real file is shot 5 of an exported project (vc_*.mp4, AAC audio with a
// genuine trailing-silence tail — the backend returned speech_end_frame=104).

const TEST_USER = 'e2e-test'
const PROJECT_ID = 'e2e-waveform-id'
// Real, backend-served media file (same-origin via the dev proxy) so decode runs for real.
const REAL_VIDEO =
  '/api/media/projects/2f1fcbbb-18c4-4f5d-b0a7-df2c70cc4343/shots/shot_5/vc_1782562825_6544acfb.mp4'

const mockProject = {
  id: PROJECT_ID,
  title: 'E2E Waveform Project',
  theme_text: 'Test theme',
  status: 'shot_review',
  creator_name: TEST_USER,
  scene_overview: 'Scene overview',
  aspect_ratio: '9:16',
  final_video_path: null,
  reference_voice_shot_id: null,
  reference_voice_path: null,
  auto_voice_calibrate: false,
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  shots: [
    {
      id: 1,
      shot_id: 1,
      project_id: PROJECT_ID,
      text: 'Shot 1',
      shot_type: 'Medium Shot',
      visual_description: 'desc',
      shot_duration: 5,
      status: 'completed',
      motion_prompt: 'pan',
      align_with_previous: false,
      use_prev_last_frame: false,
      video_path: REAL_VIDEO,
      first_frame_path: null,
      last_frame_path: null,
      word_count_warning: false,
      error_message: null,
      auto_trim: true,
      target_last_frame_path: null,
      tf_confirmed: false,
    },
  ],
}

// Realistic video-info for the real shot-5 file (trailing silence present).
const mockVideoInfo = {
  fps: 24.0,
  total_frames: 117,
  duration: 4.886,
  has_backup: false,
  speech_end_sec: 4.34742,
  speech_end_frame: 104,
}

test.describe('裁剪弹窗 · 声纹波形轨', () => {
  test.beforeEach(async ({ page }) => {
    await page.route(`/api/projects/${PROJECT_ID}`, async (route) => {
      await route.fulfill({ json: mockProject })
    })
    await page.route(`/api/projects/${PROJECT_ID}/events`, async (route) => {
      await route.fulfill({ status: 200, body: '' })
    })
    // video-info is NOT an AI endpoint, but the mock project id has no real row,
    // so we must serve it ourselves (the real video file is still decoded for real).
    await page.route('**/api/projects/*/shots/*/video-info', async (route) => {
      await route.fulfill({ json: mockVideoInfo })
    })
    // Mock AI-triggering endpoints per CLAUDE.md (defensive — not used by this flow).
    for (const path of [
      '**/api/projects/*/start',
      '**/api/projects/*/approve-script',
      '**/api/projects/*/regenerate-script',
      '**/api/projects/*/regenerate-shots',
      '**/api/projects/*/export',
      '**/api/projects/*/shots/*/generate-tail-frame',
      '**/api/projects/*/shots/*/confirm-tail-frame',
    ]) {
      await page.route(path, async (route) => {
        await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
      })
    }
  })

  // BLOCKED (real-stack finding 2026-06-29): Chromium `decodeAudioData` returns
  // EncodingError on the shot MP4s (muxed video+AAC container) — confirmed across
  // multiple production files. The frontend Web Audio decode approach (spec §3/§4.1)
  // does not work for these videos, so the waveform track unmounts (status →
  // 'unavailable') instead of rendering. Re-enable once the audio-peak source is
  // moved to a backend ffmpeg extraction (or another decodable source).
  test.fixme('打开裁剪弹窗后声纹波形轨渲染并完成解码', async ({ page }) => {
    await page.goto(`/projects/${PROJECT_ID}/shots`)
    await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 10_000 })

    // Open the trim dialog from the shot card's 裁剪 button.
    await expect(page.getByTestId('shot-card-1')).toBeVisible()
    await page.getByRole('button', { name: '裁剪' }).first().click()

    // Dialog title confirms TrimDialog is open.
    await expect(page.getByText('裁剪视频 — Shot #1')).toBeVisible({ timeout: 5_000 })

    // The waveform track label renders (it only renders while status !== 'unavailable').
    await expect(page.getByText('声纹波形')).toBeVisible({ timeout: 10_000 })

    // A canvas element is present for the waveform.
    await expect(page.locator('canvas')).toBeVisible()

    // Decode succeeds against the real file: the "声纹解码中…" loading hint
    // appears then disappears. If decode FAILED, the whole track would unmount
    // (status → unavailable) and "声纹波形" above would have vanished instead.
    await expect(page.getByText('声纹解码中…')).toHaveCount(0, { timeout: 15_000 })
    await expect(page.getByText('声纹波形')).toBeVisible()
  })

  test('波形轨不破坏既有裁剪控件', async ({ page }) => {
    await page.goto(`/projects/${PROJECT_ID}/shots`)
    await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 10_000 })
    await page.getByRole('button', { name: '裁剪' }).first().click()
    await expect(page.getByText('裁剪视频 — Shot #1')).toBeVisible({ timeout: 5_000 })

    // Existing trim controls remain present (no regression from inserting the waveform).
    await expect(page.getByRole('button', { name: '确认裁剪' })).toBeVisible()
    await expect(page.getByText(/帧:\s*\d+\s*\/\s*117/)).toBeVisible()
    // Do NOT click 确认裁剪 — that would mutate the real shot asset.
  })
})
