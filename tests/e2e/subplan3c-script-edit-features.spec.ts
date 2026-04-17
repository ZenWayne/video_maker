/**
 * Subplan 3c — Script editing features
 *
 * Tests added during the context of the following fixes:
 *   - visual_description now shown and editable in script review
 *   - AI shot edit dialog: instruction → LLM → fills text + visual fields
 *   - reset-to-script returns to SCRIPT_REVIEW (restores existing JSON, no re-generation)
 */
import { test, expect } from '@playwright/test'
import { seedProjectState, deleteProject } from '../helpers/api'

// ─── shared fixture ───────────────────────────────────────────────────────────
let projectId: string

test.beforeAll(() => {
  projectId = seedProjectState('script_review', { title: 'PW Script Edit 3c' })
})

test.afterAll(async () => {
  await deleteProject(projectId)
})

// ─── 3c.1  visual_description is rendered on each shot card ──────────────────
test('3c.1 每个 Shot 卡片显示视觉描述', async ({ page }) => {
  await page.goto(`/projects/${projectId}/script`)
  const firstCard = page.locator('[data-testid^="shot-card-"]').first()
  await expect(firstCard).toBeVisible({ timeout: 8_000 })

  // Visual description is rendered as italic grey text below the dialogue
  const visual = firstCard.locator('p.italic, p[class*="italic"]').first()
  await expect(visual).toBeVisible()
  await expect(visual).not.toBeEmpty()
})

// ─── 3c.2  edit dialog has visual_description textarea ───────────────────────
test('3c.2 编辑对话框包含视觉描述文本框', async ({ page }) => {
  await page.goto(`/projects/${projectId}/script`)
  const firstCard = page.locator('[data-testid^="shot-card-"]').first()
  await expect(firstCard).toBeVisible({ timeout: 8_000 })

  // Open edit dialog
  await firstCard.locator('button').filter({ hasText: '' }).first().click()
  const dialog = page.locator('[role="dialog"]')
  await expect(dialog).toBeVisible({ timeout: 5_000 })

  // Both "台词" and "视觉描述" labels should appear
  await expect(dialog.getByText('台词')).toBeVisible()
  await expect(dialog.getByText('视觉描述')).toBeVisible()

  // Two textareas inside the dialog
  const textareas = dialog.locator('textarea')
  expect(await textareas.count()).toBeGreaterThanOrEqual(2)
})

// ─── 3c.3  PATCH /shots/{id} is called with both text and visual_description ─
test('3c.3 保存编辑同时更新台词和视觉描述', async ({ page }) => {
  let patchBody: Record<string, unknown> | null = null

  await page.route(`**/api/projects/${projectId}/shots/*`, async (route) => {
    if (route.request().method() === 'PATCH') {
      patchBody = JSON.parse(route.request().postData() || '{}')
    }
    await route.continue()
  })

  await page.goto(`/projects/${projectId}/script`)
  const firstCard = page.locator('[data-testid^="shot-card-"]').first()
  await expect(firstCard).toBeVisible({ timeout: 8_000 })

  await firstCard.locator('button').filter({ hasText: '' }).first().click()
  const dialog = page.locator('[role="dialog"]')
  await expect(dialog).toBeVisible({ timeout: 5_000 })

  // Dialog has 3 textareas: nth(0)=AI instruction, nth(1)=台词, nth(2)=视觉描述
  const textareas = dialog.locator('textarea')
  await textareas.nth(1).fill('新台词内容')
  await textareas.nth(2).fill('新视觉描述内容')

  await dialog.getByRole('button', { name: '保存' }).click()

  await expect(async () => {
    expect(patchBody).not.toBeNull()
    expect(patchBody!.text).toBe('新台词内容')
    expect(patchBody!.visual_description).toBe('新视觉描述内容')
  }).toPass({ timeout: 5_000 })
})

// ─── 3c.4  AI suggestion section is visible in edit dialog ───────────────────
test('3c.4 编辑对话框包含 AI 建议区域', async ({ page }) => {
  await page.goto(`/projects/${projectId}/script`)
  const firstCard = page.locator('[data-testid^="shot-card-"]').first()
  await expect(firstCard).toBeVisible({ timeout: 8_000 })

  await firstCard.locator('button').filter({ hasText: '' }).first().click()
  const dialog = page.locator('[role="dialog"]')
  await expect(dialog).toBeVisible({ timeout: 5_000 })

  await expect(dialog.getByText('AI 建议')).toBeVisible()
  await expect(dialog.getByRole('button', { name: '生成' })).toBeVisible()
})

// ─── 3c.5  AI edit: mocked response fills text + visual_description fields ───
test('3c.5 AI 建议生成后填充台词和视觉描述', async ({ page }) => {
  const AI_TEXT = 'AI改写后的台词'
  const AI_VISUAL = 'AI改写后的视觉描述'

  await page.route(`**/api/projects/${projectId}/shots/*/ai-edit`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ text: AI_TEXT, visual_description: AI_VISUAL }),
    })
  })

  await page.goto(`/projects/${projectId}/script`)
  const firstCard = page.locator('[data-testid^="shot-card-"]').first()
  await expect(firstCard).toBeVisible({ timeout: 8_000 })

  await firstCard.locator('button').filter({ hasText: '' }).first().click()
  const dialog = page.locator('[role="dialog"]')
  await expect(dialog).toBeVisible({ timeout: 5_000 })

  // Dialog textareas: nth(0)=AI instruction, nth(1)=台词, nth(2)=视觉描述
  await dialog.locator('textarea').nth(0).fill('把台词改得更轻松一些')
  await dialog.getByRole('button', { name: '生成' }).click()

  // After mock response: nth(1)=台词 gets AI_TEXT, nth(2)=视觉描述 gets AI_VISUAL
  await expect(dialog.locator('textarea').nth(1)).toHaveValue(AI_TEXT, { timeout: 5_000 })
  await expect(dialog.locator('textarea').nth(2)).toHaveValue(AI_VISUAL, { timeout: 5_000 })
})

// ─── 3c.6  ai-edit endpoint is called with correct instruction ────────────────
test('3c.6 ai-edit 请求体包含用户指令', async ({ page }) => {
  let requestBody: Record<string, unknown> | null = null

  await page.route(`**/api/projects/${projectId}/shots/*/ai-edit`, async (route) => {
    requestBody = JSON.parse(route.request().postData() || '{}')
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ text: 'result', visual_description: 'result visual' }),
    })
  })

  await page.goto(`/projects/${projectId}/script`)
  const firstCard = page.locator('[data-testid^="shot-card-"]').first()
  await expect(firstCard).toBeVisible({ timeout: 8_000 })

  await firstCard.locator('button').filter({ hasText: '' }).first().click()
  const dialog = page.locator('[role="dialog"]')
  await expect(dialog).toBeVisible({ timeout: 5_000 })

  await dialog.locator('textarea').nth(0).fill('语气更自然')
  await dialog.getByRole('button', { name: '生成' }).click()

  await expect(async () => {
    expect(requestBody).not.toBeNull()
    expect(requestBody!.instruction).toBe('语气更自然')
  }).toPass({ timeout: 5_000 })
})

// ─── 3c.7  reset-to-script shows existing script, no loading spinner ─────────
test.describe('Subplan 3c.7 — 退回脚本审批恢复现有脚本', () => {
  let shotReviewId: string

  test.beforeAll(() => {
    shotReviewId = seedProjectState('shot_review', { title: 'PW ResetToScript 3c' })
  })

  test.afterAll(async () => {
    await deleteProject(shotReviewId)
  })

  test('3c.7 退回脚本审批显示已有脚本而非加载中', async ({ page }) => {
    let resetCalled = false
    // Mock reset-to-script: intercept and return success immediately
    await page.route(`**/api/projects/${shotReviewId}/reset-to-script`, async (route) => {
      resetCalled = true
      await route.fulfill({ status: 202, body: JSON.stringify({ status: 'script_review' }) })
    })

    await page.goto(`/projects/${shotReviewId}/shots`)
    await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 8_000 })

    // Register confirm handler BEFORE the click that triggers window.confirm
    page.on('dialog', (d) => d.accept())
    await page.getByRole('button', { name: /退回修改脚本/ }).click()

    // Should stay on shots page with script_review status
    await expect(page).toHaveURL(`/projects/${shotReviewId}/shots`, { timeout: 10_000 })

    // Should still show shot cards
    const cards = page.locator('[data-testid^="shot-card-"]')
    await expect(cards.first()).toBeVisible({ timeout: 8_000 })

    expect(resetCalled).toBe(true)
  })
})

// ─── 3c.8  align toggle in edit dialog works ─────────────────────────────────
test('3c.8 编辑对话框中 align 切换后保存能持久化', async ({ page }) => {
  let patchBody: Record<string, unknown> | null = null

  await page.route(`**/api/projects/${projectId}/shots/*`, async (route) => {
    if (route.request().method() === 'PATCH') {
      patchBody = JSON.parse(route.request().postData() || '{}')
    }
    await route.continue()
  })

  await page.goto(`/projects/${projectId}/script`)
  // Use shot 2 which has align_with_previous=true (shot_id=2 is the second card)
  const cards = page.locator('[data-testid^="shot-card-"]')
  await expect(cards.first()).toBeVisible({ timeout: 8_000 })

  // Open edit dialog on shot 2 (align_with_previous=true)
  const secondCard = cards.nth(1)
  await secondCard.locator('button').filter({ hasText: '' }).first().click()
  const dialog = page.locator('[role="dialog"]')
  await expect(dialog).toBeVisible({ timeout: 5_000 })

  // The align Switch should be present in the dialog
  // Look for the switch inside the dialog
  const alignSwitch = dialog.locator('[data-slot="switch"]')
  await expect(alignSwitch).toBeVisible()

  // Check initial state (shot 2 has align_with_previous=true, so switch should be checked)
  // data-checked attribute is set by base-ui when checked=true
  const initialState = await alignSwitch.getAttribute('data-checked')
  expect(initialState).not.toBeNull() // should be checked (data-checked="")

  // Click the switch to toggle it off
  await alignSwitch.click()

  // Verify the switch state changed (should now be data-unchecked)
  const afterToggle = await alignSwitch.getAttribute('data-checked')
  expect(afterToggle).toBeNull() // unchecked means no data-checked attribute

  // Save
  await dialog.getByRole('button', { name: '保存' }).click()

  // Verify the PATCH body includes align_with_previous: false
  await expect(async () => {
    expect(patchBody).not.toBeNull()
    expect(patchBody!.align_with_previous).toBe(false)
  }).toPass({ timeout: 5_000 })
})
