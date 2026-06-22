import { test, expect } from '@playwright/test'

const TEST_USER = 'e2e-test'

// Mock project in shot_review state with 2 completed shots
const mockJoinProject = {
  id: 'e2e-join-id',
  title: 'E2E Join Preview Project',
  theme_text: 'Test theme',
  status: 'shot_review',
  creator_name: TEST_USER,
  scene_overview: 'Scene overview',
  final_video_path: null,
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  shots: [
    { id: 1, shot_id: 1, project_id: 'e2e-join-id', text: 'Shot 1', motion_prompt: 'pan', align_with_previous: false, status: 'completed', video_url: '/api/media/e2e-join-id/shots/1/output.mp4', thumbnail_url: null },
    { id: 2, shot_id: 2, project_id: 'e2e-join-id', text: 'Shot 2', motion_prompt: 'zoom', align_with_previous: false, status: 'completed', video_url: '/api/media/e2e-join-id/shots/2/output.mp4', thumbnail_url: null },
  ],
}

test.describe('连贯性预览', () => {
  test.beforeEach(async ({ page }) => {
    // Mock project endpoint
    await page.route('/api/projects/e2e-join-id', async (route) => {
      await route.fulfill({ json: mockJoinProject })
    })
    // Mock SSE events endpoint
    await page.route('/api/projects/e2e-join-id/events', async (route) => {
      await route.fulfill({ status: 200, body: '' })
    })
    // Mock join-preview endpoint (avoids real ffmpeg)
    await page.route('**/api/projects/*/join-preview', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ preview_url: '/api/media/e2e-join-id/previews/join_preview.mp4?t=1' }),
      })
    })
    // Mock other AI-triggering endpoints per CLAUDE.md
    await page.route('**/api/projects/*/start', async (route) => {
      await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
    })
    await page.route('**/api/projects/*/approve-script', async (route) => {
      await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
    })
    await page.route('**/api/projects/*/regenerate-script', async (route) => {
      await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
    })
    await page.route('**/api/projects/*/regenerate-shots', async (route) => {
      await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
    })
    await page.route('**/api/projects/*/export', async (route) => {
      await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
    })
    await page.route('**/api/projects/*/shots/*/generate-tail-frame', async (route) => {
      await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
    })
    await page.route('**/api/projects/*/shots/*/confirm-tail-frame', async (route) => {
      await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
    })
  })

  test('选中 <2 个镜头时按钮禁用', async ({ page }) => {
    await page.goto('/projects/e2e-join-id/shots')
    await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 10_000 })

    const btn = page.getByTestId('join-preview-button')
    await expect(btn).toBeVisible()
    // No shots selected → disabled
    await expect(btn).toBeDisabled()

    // Select exactly 1 shot — still disabled
    const checkboxes = page.locator('[data-testid^="shot-select-"]')
    const count = await checkboxes.count()
    if (count > 0) {
      await checkboxes.first().click()
    }
    await expect(btn).toBeDisabled()
  })

  test('选中 2 个镜头后弹出播放 modal', async ({ page }) => {
    await page.goto('/projects/e2e-join-id/shots')
    await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 10_000 })

    const btn = page.getByTestId('join-preview-button')
    await expect(btn).toBeVisible()
    await expect(btn).toBeDisabled()

    // Select both shots via their checkboxes (mock project has exactly 2)
    const checkboxes = page.locator('[data-testid^="shot-select-"]')
    await expect(checkboxes).toHaveCount(2)
    await checkboxes.nth(0).click()
    await checkboxes.nth(1).click()

    // Button should now be enabled (≥2 selected)
    await expect(btn).toBeEnabled()
    await btn.click()

    // Modal should appear
    await expect(page.getByTestId('join-preview-modal')).toBeVisible({ timeout: 5_000 })

    // Video src should match join_preview.mp4
    await expect(
      page.locator('[data-testid="join-preview-modal"] video')
    ).toHaveAttribute('src', /join_preview\.mp4/)
  })

  test('关闭 modal 后 modal 消失', async ({ page }) => {
    await page.goto('/projects/e2e-join-id/shots')
    await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 10_000 })

    // Select both shots (mock project has exactly 2)
    const checkboxes = page.locator('[data-testid^="shot-select-"]')
    await expect(checkboxes).toHaveCount(2)
    await checkboxes.nth(0).click()
    await checkboxes.nth(1).click()

    const btn = page.getByTestId('join-preview-button')
    await expect(btn).toBeEnabled()
    await btn.click()
    await expect(page.getByTestId('join-preview-modal')).toBeVisible({ timeout: 5_000 })

    // Click the close button inside the modal
    await page.locator('[data-testid="join-preview-modal"] button').filter({ hasText: '关闭' }).click()
    await expect(page.getByTestId('join-preview-modal')).not.toBeVisible()
  })
})
