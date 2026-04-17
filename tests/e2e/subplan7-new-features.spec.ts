/**
 * Subplan 7 — New features: aspect_ratio, reference_image_hint, error display, scene_overview SSE
 */
import { test, expect } from '@playwright/test'
import * as path from 'path'
import { seedProjectState, deleteProject, getProject } from '../helpers/api'

// ─────────── 7a: Aspect Ratio selection on project creation ───────────

test.describe('7a — Aspect ratio selection', () => {
  let createdProjectId: string | null = null

  test.afterEach(async () => {
    if (createdProjectId) {
      await deleteProject(createdProjectId)
      createdProjectId = null
    }
  })

  test('7a.1 New project page shows aspect ratio selector, default 16:9', async ({ page }) => {
    await page.goto('/projects/new')
    const btn169 = page.getByTestId('aspect-ratio-16-9')
    const btn916 = page.getByTestId('aspect-ratio-9-16')
    await expect(btn169).toBeVisible()
    await expect(btn916).toBeVisible()
    // Default: 16:9 is active (has blue styling)
    await expect(btn169).toHaveClass(/border-blue-500/)
    await expect(btn916).not.toHaveClass(/border-blue-500/)
  })

  test('7a.2 Select 9:16 → submit → API receives aspect_ratio', async ({ page }) => {
    let requestBody: Record<string, unknown> | null = null

    await page.route('**/api/projects', async (route) => {
      if (route.request().method() === 'POST') {
        requestBody = JSON.parse(route.request().postData() || '{}')
      }
      await route.continue()
    })
    await page.route('**/api/projects/*/start', async (route) => {
      const url = route.request().url()
      const match = url.match(/\/api\/projects\/([^/]+)\/start/)
      if (match) createdProjectId = match[1]
      await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
    })

    await page.goto('/projects/new')

    // Select 9:16
    await page.getByTestId('aspect-ratio-9-16').click()
    await expect(page.getByTestId('aspect-ratio-9-16')).toHaveClass(/border-blue-500/)

    // Fill required fields
    await page.getByTestId('project-title-input').fill('PW Aspect Ratio Test')
    await page.getByTestId('project-theme-input').fill('Test theme')
    const imgPath = path.resolve(__dirname, '../fixtures/test-character.jpg')
    await page.locator('#file-input-character').setInputFiles(imgPath)
    await expect(page.locator('img[alt="预览 1"]')).toBeVisible({ timeout: 5_000 })

    await page.getByTestId('create-project-submit').click()
    await expect(page).toHaveURL(/\/projects\/.+\/shots/, { timeout: 15_000 })

    // Verify API received aspect_ratio
    expect(requestBody).not.toBeNull()
    expect(requestBody!.aspect_ratio).toBe('9:16')
  })

  test('7a.3 Project API returns aspect_ratio', async () => {
    const projectId = seedProjectState('script_review', { title: 'PW AR API Test', aspect_ratio: '9:16' })
    try {
      const project = await getProject(projectId)
      expect(project.aspect_ratio).toBe('9:16')
    } finally {
      await deleteProject(projectId)
    }
  })
})

// ─────────── 7b: Scene overview from SSE ───────────

test.describe('7b — Scene overview via SSE', () => {
  let projectId: string

  test.beforeAll(() => {
    projectId = seedProjectState('scripting', { title: 'PW SSE Overview' })
  })

  test.afterAll(async () => {
    await deleteProject(projectId)
  })

  test('7b.1 script_ready SSE event populates scene_overview', async ({ page }) => {
    // Mock SSE to deliver script_ready with scene_overview
    await page.route(`**/api/projects/${projectId}/stream*`, async (route) => {
      const event = {
        type: 'script_ready',
        data: {
          storyboard: {
            scene_overview: 'A mystical tarot reading room.',
            shots: [
              {
                shot_id: 1,
                text: 'Welcome to the reading.',
                shot_type: 'Close-up',
                visual_description: 'A candle flickers.',
                shot_duration: 4,
                align_with_previous: false,
              },
            ],
          },
        },
      }
      const sseBody = `data: ${JSON.stringify(event)}\n\n`
      await route.fulfill({
        status: 200,
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
          'Connection': 'keep-alive',
        },
        body: sseBody,
      })
    })

    await page.goto(`/projects/${projectId}/script`)

    // Scene overview should be populated from SSE event
    const overview = page.getByTestId('script-content').locator('textarea').first()
    await expect(overview).toHaveValue('A mystical tarot reading room.', { timeout: 10_000 })
  })
})

// ─────────── 7c: Reference image hint display ───────────

test.describe('7c — Reference image hint', () => {
  let projectId: string

  test.beforeAll(() => {
    projectId = seedProjectState('script_review', { title: 'PW RefHint Test' })
  })

  test.afterAll(async () => {
    await deleteProject(projectId)
  })

  test('7c.1 Disconnected shot with hint shows hint text in script page', async ({ page }) => {
    await page.goto(`/projects/${projectId}/script`)
    await expect(page.locator('[data-testid^="shot-card-"]').first()).toBeVisible({ timeout: 8_000 })

    // Shot 3 is disconnected with a reference_image_hint
    const shot3Card = page.locator('[data-testid="shot-card-3"]')
    await expect(shot3Card).toBeVisible()

    // Hint text should be visible within the card
    await expect(shot3Card.locator('text=representing the journey')).toBeVisible()
  })

  test('7c.2 Connected shot does NOT show hint', async ({ page }) => {
    await page.goto(`/projects/${projectId}/script`)
    await expect(page.locator('[data-testid^="shot-card-"]').first()).toBeVisible({ timeout: 8_000 })

    // Shot 2 is connected (align_with_previous: true), should not have hint
    const shot2Card = page.locator('[data-testid="shot-card-2"]')
    await expect(shot2Card).toBeVisible()
    await expect(shot2Card.locator('text=representing the journey')).not.toBeVisible()
  })

  test('7c.3 Hint also visible in shot review page', async ({ page }) => {
    // Use a shot_review state project
    const reviewProjectId = seedProjectState('shot_review', { title: 'PW RefHint Review' })
    try {
      await page.goto(`/projects/${reviewProjectId}/shots`)
      await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 8_000 })

      const shot3Card = page.locator('[data-testid="shot-card-3"]')
      await expect(shot3Card).toBeVisible()
      await expect(shot3Card.locator('text=representing the journey')).toBeVisible()
    } finally {
      await deleteProject(reviewProjectId)
    }
  })

  test('7c.4 Shot 1 (disconnected but first) does NOT show hint', async ({ page }) => {
    await page.goto(`/projects/${projectId}/script`)
    await expect(page.locator('[data-testid^="shot-card-"]').first()).toBeVisible({ timeout: 8_000 })

    // Shot 1 is disconnected but first, should not have hint
    const shot1Card = page.locator('[data-testid="shot-card-1"]')
    await expect(shot1Card).toBeVisible()
    await expect(shot1Card.locator('text=representing the journey')).not.toBeVisible()
  })

  test('7c.5 SSE script_ready with reference_image_hint renders correctly', async ({ page }) => {
    const sseProjectId = seedProjectState('scripting', { title: 'PW RefHint SSE' })
    try {
      // Mock SSE to deliver script_ready with reference_image_hint
      await page.route(`**/api/projects/${sseProjectId}/stream*`, async (route) => {
        const event = {
          type: 'script_ready',
          data: {
            storyboard: {
              scene_overview: 'Tarot reading session.',
              shots: [
                {
                  shot_id: 1,
                  text: 'Opening scene.',
                  shot_type: 'Wide Shot',
                  visual_description: 'A dimly lit room.',
                  shot_duration: 6,
                  align_with_previous: false,
                  reference_image_hint: null,
                },
                {
                  shot_id: 2,
                  text: 'Continued scene.',
                  shot_type: 'Medium Shot',
                  visual_description: 'The reader shuffles cards.',
                  shot_duration: 4,
                  align_with_previous: true,
                  reference_image_hint: null,
                },
                {
                  shot_id: 3,
                  text: 'Card reveal.',
                  shot_type: 'Close-up',
                  visual_description: 'Two of Cups and Five of Swords.',
                  shot_duration: 6,
                  align_with_previous: false,
                  reference_image_hint: 'Upload tarot cards: Two of Cups, Five of Swords — showing emotional misalignment',
                },
              ],
            },
          },
        }
        const sseBody = `data: ${JSON.stringify(event)}\n\n`
        await route.fulfill({
          status: 200,
          headers: {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
          },
          body: sseBody,
        })
      })

      await page.goto(`/projects/${sseProjectId}/script`)

      // After SSE, script content appears
      await expect(page.getByTestId('script-content')).toBeVisible({ timeout: 10_000 })

      // Shot 1 should not show hint
      const shot1Card = page.locator('[data-testid="shot-card-1"]')
      await expect(shot1Card).toBeVisible()
      await expect(shot1Card.locator('text=tarot cards')).not.toBeVisible()

      // Shot 2 (connected) should not show hint
      const shot2Card = page.locator('[data-testid="shot-card-2"]')
      await expect(shot2Card).toBeVisible()
      await expect(shot2Card.locator('text=tarot cards')).not.toBeVisible()

      // Shot 3 (disconnected) should show hint
      const shot3Card = page.locator('[data-testid="shot-card-3"]')
      await expect(shot3Card).toBeVisible()
      await expect(shot3Card.locator('text=tarot cards')).toBeVisible()
    } finally {
      await deleteProject(sseProjectId)
    }
  })
})

// ─────────── 7d: Failed shot error display ───────────

test.describe('7d — Failed shot error display', () => {
  let projectId: string

  test.beforeAll(() => {
    projectId = seedProjectState('shot_review_with_failures', { title: 'PW FailedShot Test' })
  })

  test.afterAll(async () => {
    await deleteProject(projectId)
  })

  test('7d.1 Failed shot shows error message', async ({ page }) => {
    await page.goto(`/projects/${projectId}/shots`)
    await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 8_000 })

    const shot3Card = page.locator('[data-testid="shot-card-3"]')
    await expect(shot3Card).toBeVisible()

    // Error message should be displayed
    await expect(shot3Card.locator('text=INVALID_ARGUMENT')).toBeVisible()
  })

  test('7d.2 Failed shot shows regenerate button', async ({ page }) => {
    await page.goto(`/projects/${projectId}/shots`)
    await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 8_000 })

    const shot3Card = page.locator('[data-testid="shot-card-3"]')
    await expect(shot3Card).toBeVisible()

    // Regenerate button should be visible for failed shots
    await expect(shot3Card.getByRole('button', { name: /重新生成/ })).toBeVisible()
  })
})

// ─────────── 7e: Edit pending shots during shot_generating ───────────

test.describe('7e — Edit pending shots during generation', () => {
  let projectId: string

  test.beforeAll(() => {
    projectId = seedProjectState('shot_generating', { title: 'PW EditDuringGen' })
  })

  test.afterAll(async () => {
    await deleteProject(projectId)
  })

  test('7e.1 Pending shots are editable during shot_generating', async ({ page }) => {
    await page.goto(`/projects/${projectId}/shots`)
    await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 8_000 })

    // All shots are pending in shot_generating state (seeded as pending)
    // They should show the review variant with edit capabilities (edit button visible)
    const shot1Card = page.locator('[data-testid="shot-card-1"]')
    await expect(shot1Card).toBeVisible()

    // Should display shot text (review variant shows text, generating variant doesn't)
    await expect(shot1Card.locator('text=主角登场')).toBeVisible()
  })

  test('7e.2 Disconnected pending shot shows reference image upload during generation', async ({ page }) => {
    await page.goto(`/projects/${projectId}/shots`)
    await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 8_000 })

    // Shot 3 is disconnected (align_with_previous: false), should show ref image upload
    const shot3Card = page.locator('[data-testid="shot-card-3"]')
    await expect(shot3Card).toBeVisible()
    await expect(shot3Card.getByRole('button', { name: /参考图/ })).toBeVisible()
  })
})
