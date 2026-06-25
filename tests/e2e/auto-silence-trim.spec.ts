/**
 * Auto silence trim — mocked detect-silence endpoint.
 * Verifies the 静音裁剪 button moves the slider to the suggested frame
 * and that the dialog stays open (suggest-only, no auto-apply).
 * Also verifies the "无尾部静音可裁剪" notice when has_silence is false.
 */
import { test, expect } from '@playwright/test'
import { seedProjectState, deleteProject } from '../helpers/api'

// ─────────── shared fixture ───────────
let projectId: string

test.beforeAll(() => {
  projectId = seedProjectState('shot_review', { title: 'PW SilenceTrim' })
})

test.afterAll(async () => {
  await deleteProject(projectId)
})

// ─────────── helper ───────────

/**
 * Navigate to the shots page, open the TrimDialog for the first shot, and
 * wait for the dialog title to be visible.  Callers must set up any
 * route mocks BEFORE calling this helper so that the mocks are registered
 * before the dialog triggers its API requests.
 */
async function openTrimDialogForFirstShot(page: import('@playwright/test').Page, pid: string) {
  // Mock video-info endpoint — TrimDialog calls this on open to seed fps/totalFrames.
  // We use the same values as the detect-silence mock payloads below (fps 24, 200 frames).
  await page.route(`**/api/projects/${pid}/shots/*/video-info`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        fps: 24,
        total_frames: 200,
        duration: 8.33,
        has_backup: false,
      }),
    })
  })

  // Mock AI-triggering endpoints so no real AI is hit
  await page.route(`**/api/projects/${pid}/start`, async (route) => {
    await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
  })
  await page.route(`**/api/projects/${pid}/approve-script`, async (route) => {
    await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
  })
  await page.route(`**/api/projects/${pid}/regenerate-script`, async (route) => {
    await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
  })
  await page.route(`**/api/projects/${pid}/regenerate-shots`, async (route) => {
    await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
  })
  await page.route(`**/api/projects/${pid}/shots/*/generate-tail-frame`, async (route) => {
    await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
  })
  await page.route(`**/api/projects/${pid}/shots/*/confirm-tail-frame`, async (route) => {
    await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
  })
  await page.route(`**/api/projects/${pid}/export`, async (route) => {
    await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
  })
  await page.route(`**/api/projects/${pid}/shots/*/align-tail-frame`, async (route) => {
    await route.fulfill({ status: 200, body: JSON.stringify({ status: 'ok' }) })
  })

  await page.goto(`/projects/${pid}/shots`)
  const shotsList = page.getByTestId('shots-list')
  await expect(shotsList).toBeVisible({ timeout: 8_000 })

  // Click the 裁剪 button on the first shot card to open TrimDialog
  const firstCard = shotsList.locator('[data-testid^="shot-card"]').first()
  await expect(firstCard).toBeVisible()
  await firstCard.getByRole('button', { name: '裁剪' }).click()

  // Wait for the dialog title
  await expect(page.getByText(/裁剪视频/)).toBeVisible({ timeout: 8_000 })
}

// ─────────── tests ───────────

test.describe('Auto silence trim (suggest-only)', () => {
  test('静音裁剪 moves the slider to the suggested frame', async ({ page }) => {
    // Register detect-silence mock BEFORE opening the dialog
    await page.route(`**/api/projects/${projectId}/shots/*/detect-silence`, async (route) => {
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

    await openTrimDialogForFirstShot(page, projectId)

    await page.getByRole('button', { name: '静音裁剪' }).click()

    // Slider (input[type=range]) should now read the suggested frame.
    await expect(page.locator('input[type="range"]')).toHaveValue('120')
    // Frame readout reflects the suggestion.
    await expect(page.getByText(/帧:\s*120\s*\/\s*200/)).toBeVisible()
    // Dialog stays open (suggest-only, not applied): 确认裁剪 still present.
    await expect(page.getByRole('button', { name: '确认裁剪' })).toBeVisible()
  })

  test('shows a notice when there is no trailing silence', async ({ page }) => {
    await page.route(`**/api/projects/${projectId}/shots/*/detect-silence`, async (route) => {
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

    await openTrimDialogForFirstShot(page, projectId)
    await page.getByRole('button', { name: '静音裁剪' }).click()

    await expect(page.getByText('无尾部静音可裁剪')).toBeVisible()
  })
})
