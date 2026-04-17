/**
 * Subplan 2 — 脚本生成等待
 * Fixture: scripting 状态的项目
 * 通过 route mock 注入 SSE 事件，验证页面响应。
 */
import { test, expect } from '@playwright/test'
import { seedProjectState, deleteProject } from '../helpers/api'

let projectId: string

test.beforeAll(() => {
  projectId = seedProjectState('scripting', { title: 'PW Scripting Test' })
})

test.afterAll(async () => {
  await deleteProject(projectId)
})

test('2.1 进入脚本页显示生成中状态，审批按钮不存在', async ({ page }) => {
  await page.goto(`/projects/${projectId}/script`)
  await expect(page.getByTestId('script-loading')).toBeVisible()
  await expect(page.getByTestId('approve-script-button')).not.toBeVisible()
})

test('2.2-2.3 SSE script_ready 事件 → 页面切换到审批视图', async ({ page }) => {
  // Mock SSE stream to immediately fire script_ready
  await page.route(`**/api/projects/${projectId}/stream*`, async (route) => {
    const shots = [
      {
        id: 1, shot_id: 1, project_id: projectId,
        text: '主角登场', shot_type: 'Wide Shot', visual_description: '...',
        shot_duration: 6, status: 'pending', align_with_previous: false,
        motion_prompt: null, first_frame_path: null, video_path: null,
        last_frame_path: null, word_count_warning: false, error_message: null,
      },
    ]
    const sseBody = [
      `data: ${JSON.stringify({ type: 'script_ready', data: { storyboard: { shots } } })}\n\n`,
    ].join('')

    await route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
      body: sseBody,
    })
  })

  await page.goto(`/projects/${projectId}/script`)

  // After SSE, loading state disappears and script content appears
  await expect(page.getByTestId('script-content')).toBeVisible({ timeout: 10_000 })
  await expect(page.getByTestId('approve-script-button')).toBeVisible()
})
