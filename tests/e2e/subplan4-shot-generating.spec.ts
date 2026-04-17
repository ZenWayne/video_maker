/**
 * Subplan 4 — 分镜生成等待
 * Fixture: shot_generating 状态（有 shots）
 */
import { test, expect } from '@playwright/test'
import { seedProjectState, deleteProject } from '../helpers/api'

let projectId: string

test.beforeAll(() => {
  projectId = seedProjectState('shot_generating', { title: 'PW ShotGenerating Test' })
})

test.afterAll(async () => {
  await deleteProject(projectId)
})

test('4.1 进入分镜页展示生成进度 ProgressStream 可见', async ({ page }) => {
  await page.goto(`/projects/${projectId}/shots`)
  // ProgressStream renders a progress bar
  await expect(page.locator('[role="progressbar"]').first()).toBeVisible({ timeout: 8_000 })
})

test('4.2-4.3 SSE all_shots_ready → 视频播放器出现，导出按钮激活', async ({ page }) => {
  // Mock SSE: immediately fire all_shots_ready
  await page.route(`**/api/projects/${projectId}/stream*`, async (route) => {
    const sseBody = [
      `data: ${JSON.stringify({ type: 'all_shots_ready', data: {} })}\n\n`,
    ].join('')
    await route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
      body: sseBody,
    })
  })

  // Mock getProject to return shot_review with completed shots
  await page.route(`**/api/projects/${projectId}`, async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: projectId, title: 'PW ShotGenerating Test', theme_text: 'test',
        creator_name: 'pw-test', status: 'shot_review',
        scene_overview: '测试场景', storyboard_path: null,
        final_video_path: null, error_message: null,
        created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
        reference_images: [],
        shots: [
          {
            id: 1, shot_id: 1, project_id: projectId,
            text: '主角登场', shot_type: 'Wide Shot', visual_description: '...',
            shot_duration: 6, status: 'completed', align_with_previous: false,
            motion_prompt: null, first_frame_path: null,
            video_path: '/api/projects/test/assets/shots/shot_1/output.mp4',
            last_frame_path: null, word_count_warning: false, error_message: null,
            created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
          },
        ],
      }),
    })
  })

  await page.goto(`/projects/${projectId}/shots`)

  // After all_shots_ready, export button should become enabled
  await expect(page.getByTestId('export-button')).toBeEnabled({ timeout: 10_000 })
})
