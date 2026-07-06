/**
 * 统一图片生成候选流。
 * - 生成触发（AI 计费点）：仅 stub POST image-candidates，断言弹窗发出的请求参数。
 * - 采纳链路：候选行 + 真实图片直插 DB（seedImageCandidate），adopt 走真实后端，
 *   断言真实 GET /api/projects/{id} 反映新的 target_last_frame_path / tf_status / adopted_at。
 */
import { test, expect } from '@playwright/test'
import {
  createProject,
  uploadReferenceImage,
  seedShotReview,
  seedImageCandidate,
  deleteProject,
  getProject,
} from '../helpers/api'

let projectId: string

test.beforeAll(async () => {
  projectId = await createProject('PW ImageCandidates', 'Playwright e2e 候选采纳链路')
  await uploadReferenceImage(projectId)
  seedShotReview(projectId, 1)
})

test.afterAll(async () => {
  await deleteProject(projectId)
})

test('生成弹窗从关键帧下拉打开并发出真实形状的创建请求', async ({ page }) => {
  let captured: Record<string, string> | null = null
  await page.route(`**/api/projects/${projectId}/shots/*/image-candidates`, async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    const buf = route.request().postDataBuffer()
    captured = Object.fromEntries(
      [...buf!.toString().matchAll(/name="([^"]+)"\r\n\r\n([^\r]*)/g)].map(m => [m[1], m[2]]),
    )
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({
        status: 'queued',
        candidate: {
          id: 'stub', shot_id: 1, slot: 'tail_frame', status: 'generating',
          file_path: null, prompt_source: 'custom', custom_prompt: '自定义提示词',
          error: null, created_at: new Date().toISOString(), adopted_at: null,
        },
      }),
    })
  })

  await page.goto(`/projects/${projectId}/shots`)
  await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 8_000 })

  // 打开尾帧槽位的关键帧下拉 → 生成尾帧…
  await page.getByText('尾帧', { exact: true }).first().click()
  await page.getByText('生成尾帧…').click()
  await expect(page.getByText('生成图片 — Shot #1')).toBeVisible()

  await page.getByPlaceholder(/少女转身面向大海/).fill('自定义提示词')
  await page.getByRole('button', { name: /生成 1 张候选/ }).click()
  await expect.poll(() => captured).not.toBeNull()
  expect(captured!.slot).toBe('tail_frame')
  expect(captured!.custom_prompt).toBe('自定义提示词')
})

test('采纳候选走真实后端并写入尾帧槽位', async ({ page }) => {
  seedImageCandidate(projectId, 1, 'tail_frame')

  await page.goto(`/projects/${projectId}/shots`)
  await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 8_000 })

  await page.getByText('尾帧', { exact: true }).first().click()
  await page.getByText('生成尾帧…').click()
  await expect(page.getByText('候选画廊 · 尾帧')).toBeVisible()

  await page.getByRole('button', { name: '采纳' }).first().click()
  await expect(page.getByText('已采纳')).toBeVisible()

  // 真实后端状态：target_last_frame_path 已写入且 tf_status=done
  const proj = await getProject(projectId) as { shots: Array<Record<string, unknown>> }
  const shot1 = proj.shots.find(s => s.shot_id === 1)!
  expect(shot1.target_last_frame_path).toBeTruthy()
  expect(shot1.tf_status).toBe('done')
  const cands = shot1.image_candidates as Array<Record<string, unknown>>
  expect(cands.some(c => c.adopted_at)).toBe(true)
})
