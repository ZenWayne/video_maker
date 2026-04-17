/**
 * Subplan 1 — 新建项目
 * 完整调用真实 API，afterEach 清理。
 */
import { test, expect } from '@playwright/test'
import * as path from 'path'
import { deleteProject } from '../helpers/api'

let createdProjectId: string | null = null

test.afterEach(async () => {
  if (createdProjectId) {
    await deleteProject(createdProjectId)
    createdProjectId = null
  }
})

test('1.1 点击新建项目跳转 /projects/new', async ({ page }) => {
  await page.goto('/')
  await page.getByTestId('new-project-button').click()
  await expect(page).toHaveURL('/projects/new')
})

test('1.2-1.5 填写表单、上传图片、提交 → 跳转脚本页', async ({ page }) => {
  // Mock /start so no AI worker is triggered (no billing)
  await page.route('**/api/projects/*/start', async (route) => {
    const url = route.request().url()
    const match = url.match(/\/api\/projects\/([^/]+)\/start/)
    if (match) createdProjectId = match[1]
    await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
  })

  await page.goto('/projects/new')

  // 1.2 填写标题和主题
  await page.getByTestId('project-title-input').fill('PW Test Project')
  await page.getByTestId('project-theme-input').fill('Playwright 自动化测试主题')

  // 1.3 上传角色参考图（直接向 hidden input 设置文件）
  const imgPath = path.resolve(__dirname, '../fixtures/test-character.jpg')
  await page.locator('#file-input-character').setInputFiles(imgPath)

  // 验证缩略图预览出现
  await expect(page.locator('img[alt="预览 1"]')).toBeVisible({ timeout: 5_000 })

  // 1.4 点击提交
  let createCalled = false
  let startCalled = false
  await page.route('**/api/projects', async (route) => {
    if (route.request().method() === 'POST') createCalled = true
    await route.continue()
  })
  // /start is already mocked above; capture the flag here too
  await page.route('**/api/projects/*/start', async (route) => {
    startCalled = true
    await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
  })

  await page.getByTestId('create-project-submit').click()

  // 1.5 成功后跳转到分镜页
  await expect(page).toHaveURL(/\/projects\/.+\/shots/, { timeout: 15_000 })
  expect(createCalled).toBe(true)
  expect(startCalled).toBe(true)
})
