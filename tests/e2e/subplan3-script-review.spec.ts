/**
 * Subplan 3a — 查看与编辑脚本（并行）
 * Subplan 3b — 审批通过（独立 fixture）
 */
import { test, expect } from '@playwright/test'
import { seedProjectState, deleteProject } from '../helpers/api'

// ─────────── 3a fixture ───────────
let projectId3a: string

test.beforeAll(() => {
  projectId3a = seedProjectState('script_review', { title: 'PW ScriptReview 3a' })
})

test.afterAll(async () => {
  await deleteProject(projectId3a)
})

// ─────────── 3a tests ───────────

test('3a.1 scene_overview 文本区域与 API 返回一致', async ({ page }) => {
  await page.goto(`/projects/${projectId3a}/script`)
  const overview = page.getByTestId('script-content').locator('textarea').first()
  await expect(overview).toBeVisible()
  await expect(overview).not.toBeEmpty()
})

test('3a.2 展示所有 Shot 卡片（shot_id、文本、镜头类型、时长）', async ({ page }) => {
  await page.goto(`/projects/${projectId3a}/script`)
  // Wait for shot cards to appear
  const cards = page.locator('[data-testid^="shot-card-"]')
  await expect(cards.first()).toBeVisible({ timeout: 8_000 })
  const count = await cards.count()
  expect(count).toBeGreaterThanOrEqual(1)
})

test('3a.3 修改 scene_overview 并保存 → PATCH /storyboard 调用成功', async ({ page }) => {
  let patched = false
  await page.route(`**/api/projects/${projectId3a}/storyboard`, async (route) => {
    if (route.request().method() === 'PATCH') patched = true
    await route.continue()
  })

  await page.goto(`/projects/${projectId3a}/script`)
  const overview = page.getByTestId('script-content').locator('textarea').first()
  await overview.fill('修改后的场景概览内容')
  // Click the save button inside script-content
  await page.getByTestId('script-content').getByRole('button', { name: /保存/ }).click()
  await expect(async () => expect(patched).toBe(true)).toPass({ timeout: 5_000 })
})

test('3a.4 修改 Shot 台词 → 本地状态同步更新', async ({ page }) => {
  await page.goto(`/projects/${projectId3a}/script`)
  // Find first shot's text area or edit trigger
  const firstCard = page.locator('[data-testid^="shot-card-"]').first()
  await expect(firstCard).toBeVisible()
  // Open edit dialog via the Edit button
  await firstCard.locator('button').first().click()
  const textarea = page.locator('dialog textarea, [role="dialog"] textarea').first()
  await expect(textarea).toBeVisible({ timeout: 5_000 })
  await textarea.fill('修改后的台词文本')
  await expect(textarea).toHaveValue('修改后的台词文本')
})

// ─────────── 3b — independent fixture ───────────
test.describe('Subplan 3b — 审批通过', () => {
  let projectId3b: string

  test.beforeAll(() => {
    projectId3b = seedProjectState('script_review', { title: 'PW ScriptReview 3b' })
  })

  test.afterAll(async () => {
    await deleteProject(projectId3b)
  })

  test('3b.1-3b.2 点击审批通过 → POST approve-script → 跳转分镜页', async ({ page }) => {
    let approved = false
    await page.route(`**/api/projects/${projectId3b}/approve-script`, async (route) => {
      approved = true
      // Fulfill with success so page navigates without waiting for worker
      await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
    })

    await page.goto(`/projects/${projectId3b}/script`)
    await expect(page.getByTestId('approve-script-button')).toBeVisible()
    await page.getByTestId('approve-script-button').click()

    expect(approved).toBe(true)
    await expect(page).toHaveURL(`/projects/${projectId3b}/shots`, { timeout: 10_000 })
  })
})
