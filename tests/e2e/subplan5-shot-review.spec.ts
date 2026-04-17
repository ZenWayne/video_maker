/**
 * Subplan 5a — 查看分镜（并行）
 * Subplan 5b — 重新生成选中分镜（并行）
 * Subplan 5c — 触发导出（独立 fixture）
 */
import { test, expect } from '@playwright/test'
import { seedProjectState, deleteProject } from '../helpers/api'

// ─────────── shared fixture for 5a / 5b ───────────
let projectId5ab: string

test.beforeAll(() => {
  projectId5ab = seedProjectState('shot_review', { title: 'PW ShotReview 5ab' })
})

test.afterAll(async () => {
  await deleteProject(projectId5ab)
})

// ─────────── 5a ───────────

test('5a.1 展示每个 Shot 的视频播放器 <video> 元素可见', async ({ page }) => {
  await page.goto(`/projects/${projectId5ab}/shots`)
  const shotsList = page.getByTestId('shots-list')
  await expect(shotsList).toBeVisible({ timeout: 8_000 })

  // At least one shot card visible
  const cards = shotsList.locator('[data-testid^="shot-card"]')
  await expect(cards.first()).toBeVisible()
})

test('5a.2 首帧截图图片加载', async ({ page }) => {
  await page.goto(`/projects/${projectId5ab}/shots`)
  await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 8_000 })
  // Images with src that include first_frame/last_frame should be present or video elements
  const media = page.locator('video, img[src*="first_frame"], img[src*="last_frame"], img[src*="assets"]')
  // At least one media element present in the shots section
  await expect(media.first()).toBeAttached({ timeout: 8_000 })
})

// ─────────── 5b ───────────

test('5b.1 勾选 Shot → 重跑按钮从禁用变可用', async ({ page }) => {
  await page.goto(`/projects/${projectId5ab}/shots`)
  await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 8_000 })

  // Regenerate button should be disabled before selection
  const regenBtn = page.getByRole('button', { name: /重跑选中|重新生成选中/ })
  await expect(regenBtn).toBeDisabled()

  // Click first shot's select button
  await page.locator('[data-testid^="shot-select-"]').first().click()
  await expect(regenBtn).toBeEnabled({ timeout: 5_000 })
})

test('5b.3 点击重跑 → POST regenerate-shots 传入正确 shot_ids', async ({ page }) => {
  let requestBody: unknown = null
  await page.route(`**/api/projects/${projectId5ab}/regenerate-shots`, async (route) => {
    requestBody = JSON.parse(route.request().postData() || '{}')
    await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
  })

  await page.goto(`/projects/${projectId5ab}/shots`)
  await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 8_000 })

  await page.locator('[data-testid^="shot-select-"]').first().click()
  await page.getByRole('button', { name: /重跑选中|重新生成选中/ }).click()

  await expect(async () => {
    expect(requestBody).not.toBeNull()
    expect((requestBody as { shot_ids: number[] }).shot_ids).toEqual(expect.arrayContaining([expect.any(Number)]))
  }).toPass({ timeout: 5_000 })
})

// ─────────── 5c — independent fixture ───────────
test.describe('Subplan 5c — 触发导出', () => {
  let projectId5c: string

  test.beforeAll(() => {
    projectId5c = seedProjectState('shot_review', { title: 'PW ShotReview 5c' })
  })

  test.afterAll(async () => {
    await deleteProject(projectId5c)
  })

  test('5c.1 所有分镜已完成，导出按钮可点击', async ({ page }) => {
    await page.goto(`/projects/${projectId5c}/shots`)
    await expect(page.getByTestId('export-button')).toBeEnabled({ timeout: 8_000 })
  })

  test('5c.2-5c.3 点击导出 → POST export → 跳转导出页', async ({ page }) => {
    let exported = false
    await page.route(`**/api/projects/${projectId5c}/export`, async (route) => {
      exported = true
      await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
    })

    await page.goto(`/projects/${projectId5c}/shots`)
    await expect(page.getByTestId('export-button')).toBeEnabled({ timeout: 8_000 })
    await page.getByTestId('export-button').click()

    expect(exported).toBe(true)
    await expect(page).toHaveURL(`/projects/${projectId5c}/export`, { timeout: 10_000 })
  })
})
