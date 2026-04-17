/**
 * Subplan 2b — SSE 集成测试（真实 SSE 路径）
 *
 * 与 subplan2 的区别：不 mock SSE route，而是通过后端 debug 接口往 Redis
 * publish 事件，让事件走真实的 Redis → SSE → 浏览器路径。
 *
 * 这样能捕获后端事件格式错误（如字段名错误、缺少 data 包装等），
 * 而纯 mock SSE 的测试覆盖不到这类 bug。
 */

import { test, expect } from '@playwright/test'
import { createProject, uploadReferenceImage, deleteProject } from '../helpers/api'

const BACKEND = 'http://localhost:8002'
const USER = 'pw-test'

async function publishEvent(projectId: string, event: Record<string, unknown>) {
  const res = await fetch(`${BACKEND}/api/projects/${projectId}/debug/publish-event`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-User-Name': USER },
    body: JSON.stringify({ event }),
  })
  if (!res.ok) throw new Error(`publishEvent failed: ${res.status} ${await res.text()}`)
}

let projectId: string

test.beforeAll(async () => {
  projectId = await createProject('PW SSE Integration', 'Playwright SSE 集成测试')
  await uploadReferenceImage(projectId)
})

test.afterAll(async () => {
  await deleteProject(projectId)
})

test('2b.1 state_snapshot 事件经真实 SSE 路径不报解析错误', async ({ page }) => {
  const consoleErrors: string[] = []
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(msg.text())
  })

  await page.goto(`/projects/${projectId}/script`)
  await page.waitForTimeout(300)

  // Inject state_snapshot via real Redis → SSE path
  await publishEvent(projectId, {
    type: 'state_snapshot',
    data: {
      project: {
        id: projectId,
        title: 'PW SSE Integration',
        theme_text: 'Playwright SSE 集成测试',
        creator_name: USER,
        status: 'draft',
        scene_overview: null,
        final_video_path: null,
        error_message: null,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
      shots: [],
    },
  })

  await page.waitForTimeout(500)

  // Verify no "Failed to parse SSE message" errors (the bug this test guards against)
  const sseErrors = consoleErrors.filter((e) => e.includes('Failed to parse SSE message'))
  expect(sseErrors).toHaveLength(0)
})

test('2b.2 script_ready 事件走真实 SSE 路径 → 页面切换到审批视图', async ({ page }) => {
  // Mock /start so no real AI call (per AGENTS.md rule)
  await page.route('**/api/projects/*/start', async (route) => {
    await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
  })

  await page.goto(`/projects/${projectId}/script`)

  // Wait for SSE connection to be established
  await page.waitForTimeout(300)

  // Inject script_ready via real Redis → SSE path (not page.route mock)
  const shots = [
    {
      id: 1,
      shot_id: 1,
      project_id: projectId,
      text: '主角登场，镜头缓缓推近',
      shot_type: 'Wide Shot',
      visual_description: '清晨的街道，主角站在路口',
      shot_duration: 6,
      status: 'pending',
      align_with_previous: false,
      motion_prompt: null,
      first_frame_path: null,
      video_path: null,
      last_frame_path: null,
      word_count_warning: false,
      error_message: null,
    },
    {
      id: 2,
      shot_id: 2,
      project_id: projectId,
      text: '主角转身，面向镜头微笑',
      shot_type: 'Medium Shot',
      visual_description: '特写主角表情，背景虚化',
      shot_duration: 4,
      status: 'pending',
      align_with_previous: true,
      motion_prompt: null,
      first_frame_path: null,
      video_path: null,
      last_frame_path: null,
      word_count_warning: false,
      error_message: null,
    },
  ]

  await publishEvent(projectId, {
    type: 'script_ready',
    data: {
      storyboard: {
        scene_overview: '清晨城市街头，主角踏上新的旅程',
        shots,
      },
    },
  })

  // Verify page switched to script review mode
  await expect(page.getByTestId('script-content')).toBeVisible({ timeout: 5_000 })
  await expect(page.getByTestId('approve-script-button')).toBeVisible()

  // Verify shot cards rendered
  await expect(page.getByTestId('approve-script-button')).toBeEnabled()
})
