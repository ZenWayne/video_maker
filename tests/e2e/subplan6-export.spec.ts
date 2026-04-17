/**
 * Subplan 6 — 导出完成与下载
 * Fixture: exporting → 通过 SSE mock 切换到 exported
 */
import { test, expect } from '@playwright/test'
import { seedProjectState, deleteProject } from '../helpers/api'

let projectId: string

test.beforeAll(() => {
  projectId = seedProjectState('exporting', { title: 'PW Export Test' })
})

test.afterAll(async () => {
  await deleteProject(projectId)
})

test('6.1 进入导出页显示导出进度', async ({ page }) => {
  await page.goto(`/projects/${projectId}/export`)
  await expect(page.getByTestId('export-progress')).toBeVisible({ timeout: 8_000 })
  // Status badge shows "导出中"
  await expect(page.getByText('导出中')).toBeVisible()
})

test('6.2 SSE export_done → 最终视频播放器和下载按钮出现', async ({ page }) => {
  const fakeVideoPath = `/api/projects/${projectId}/final-video`

  // Mock SSE to fire export_done immediately
  await page.route(`**/api/projects/${projectId}/stream*`, async (route) => {
    const sseBody = `data: ${JSON.stringify({
      type: 'export_done',
      data: { final_video_path: fakeVideoPath },
    })}\n\n`
    await route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
      body: sseBody,
    })
  })

  await page.goto(`/projects/${projectId}/export`)

  // After export_done, download button should appear
  await expect(page.getByTestId('download-video-button')).toBeVisible({ timeout: 10_000 })
  await expect(page.getByTestId('download-video-button')).toBeEnabled()
})

test('6.3 点击下载触发 final-video 请求', async ({ page }) => {
  // Seed an exported project for this test
  const exportedId = seedProjectState('exported', { title: 'PW Exported Test' })

  try {
    await page.goto(`/projects/${exportedId}/export`)
    await expect(page.getByTestId('download-video-button')).toBeEnabled({ timeout: 8_000 })

    // window.open opens a new tab — intercept at context level and capture the popup URL
    const [popup] = await Promise.all([
      page.context().waitForEvent('page'),
      page.getByTestId('download-video-button').click(),
    ])

    // The popup navigates to the final-video endpoint
    expect(popup.url()).toMatch(/\/api\/projects\/.+\/final-video/)
  } finally {
    await deleteProject(exportedId)
  }
})
