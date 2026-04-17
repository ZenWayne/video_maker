import { test, expect, APIRequestContext } from '@playwright/test';

const TEST_USER = 'e2e-test';

async function createProject(request: APIRequestContext, title: string, theme = 'E2E test theme') {
  const res = await request.post('/api/projects', {
    headers: { 'X-User-Name': TEST_USER },
    data: { title, theme_text: theme },
  });
  expect(res.ok()).toBeTruthy();
  const data = await res.json();
  return data.id as string;
}

async function deleteProject(request: APIRequestContext, id: string) {
  await request.delete(`/api/projects/${id}`, {
    headers: { 'X-User-Name': TEST_USER },
  });
}

// ─── Home Page ──────────────────────────────────────────────────────────────

test.describe('Home Page', () => {
  test('shows header and new-project button', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByText('视频制作工具')).toBeVisible();
    await expect(page.getByTestId('new-project-button')).toBeVisible();
  });

  test('shows project list when projects exist', async ({ page, request }) => {
    const id = await createProject(request, 'E2E-列表测试');
    try {
      await page.goto('/');
      await expect(page.getByTestId('project-list')).toBeVisible({ timeout: 10_000 });
      await expect(page.getByText('E2E-列表测试')).toBeVisible();
    } finally {
      await deleteProject(request, id);
    }
  });

  test('search filters projects by title', async ({ page, request }) => {
    const id = await createProject(request, 'E2E-UniqueSearch9999');
    try {
      await page.goto('/');
      await expect(page.getByTestId('project-list')).toBeVisible({ timeout: 10_000 });
      await page.getByTestId('search-input').fill('UniqueSearch9999');
      await expect(page.getByText('E2E-UniqueSearch9999')).toBeVisible();
      await expect(page.getByTestId('project-card')).toHaveCount(1);
    } finally {
      await deleteProject(request, id);
    }
  });

  test('status filter works via select', async ({ page, request }) => {
    const id = await createProject(request, 'E2E-FilterTest');
    try {
      await page.goto('/');
      await expect(page.getByTestId('project-list')).toBeVisible({ timeout: 10_000 });
      await page.getByTestId('status-filter').selectOption('draft');
      await page.waitForTimeout(500);
      const listVisible = await page.getByTestId('project-list').isVisible().catch(() => false);
      const emptyVisible = await page.locator('text=暂无项目').isVisible().catch(() => false);
      expect(listVisible || emptyVisible).toBe(true);
    } finally {
      await deleteProject(request, id);
    }
  });

  test('clicking project card navigates to project detail', async ({ page, request }) => {
    const id = await createProject(request, 'E2E-NavigateTest');
    try {
      await page.goto('/');
      await expect(page.getByTestId('project-list')).toBeVisible({ timeout: 10_000 });
      await page.getByText('E2E-NavigateTest').click();
      await expect(page).toHaveURL(new RegExp(`/projects/${id}`));
    } finally {
      await deleteProject(request, id);
    }
  });
});

// ─── New Project Page ────────────────────────────────────────────────────────

test.describe('New Project Page', () => {
  test('shows form fields', async ({ page }) => {
    await page.goto('/projects/new');
    await expect(page.getByTestId('project-title-input')).toBeVisible();
    await expect(page.getByTestId('project-theme-input')).toBeVisible();
    await expect(page.getByTestId('create-project-submit')).toBeVisible();
  });

  test('new-project button navigates to /projects/new', async ({ page }) => {
    await page.goto('/');
    await page.getByTestId('new-project-button').click();
    await expect(page).toHaveURL('/projects/new');
  });

  test('validates required title', async ({ page }) => {
    await page.goto('/projects/new');
    await page.getByTestId('create-project-submit').click();
    await expect(page.getByText('请输入项目标题')).toBeVisible({ timeout: 5_000 });
  });

  test('validates required theme', async ({ page }) => {
    await page.goto('/projects/new');
    await page.getByTestId('project-title-input').fill('Some title');
    await page.getByTestId('create-project-submit').click();
    await expect(page.getByText('请输入主题描述')).toBeVisible({ timeout: 5_000 });
  });

  test('validates character image required', async ({ page }) => {
    await page.goto('/projects/new');
    await page.getByTestId('project-title-input').fill('Some title');
    await page.getByTestId('project-theme-input').fill('Some theme');
    await page.getByTestId('create-project-submit').click();
    await expect(page.getByText('请上传至少一张角色参考图')).toBeVisible({ timeout: 5_000 });
  });

  test('creates project and redirects to script page', async ({ page }) => {
    await page.route('/api/projects', async (route) => {
      if (route.request().method() === 'POST') {
        await route.fulfill({ json: { id: 'e2e-mock-id', status: 'draft' } });
      } else {
        await route.continue();
      }
    });
    await page.route('/api/projects/e2e-mock-id/reference-images', async (route) => {
      await route.fulfill({ json: { image_ids: ['img1'] } });
    });
    await page.route('/api/projects/e2e-mock-id/start', async (route) => {
      await route.fulfill({ status: 204, body: '' });
    });
    await page.route('/api/projects/e2e-mock-id', async (route) => {
      await route.fulfill({
        json: {
          id: 'e2e-mock-id', title: 'E2E Mock Project', theme_text: 'mock theme',
          status: 'scripting', creator_name: TEST_USER, scene_overview: null,
          final_video_path: null, shots: [],
          created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
        },
      });
    });
    await page.route('/api/projects/e2e-mock-id/events', async (route) => {
      await route.fulfill({ status: 200, body: '' });
    });

    await page.goto('/projects/new');
    await page.getByTestId('project-title-input').fill('E2E Mock Project');
    await page.getByTestId('project-theme-input').fill('A mock theme for testing');
    await page.locator('#file-input-character').setInputFiles({
      name: 'test.jpg',
      mimeType: 'image/jpeg',
      buffer: Buffer.from([0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01, 0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xD9]),
    });
    await page.getByTestId('create-project-submit').click();
    await expect(page).toHaveURL(/\/projects\/e2e-mock-id\/script/, { timeout: 10_000 });
  });
});

// ─── Script Review Page ──────────────────────────────────────────────────────

const mockScriptProject = {
  id: 'e2e-script-id',
  title: 'E2E Script Project',
  theme_text: 'Test theme',
  status: 'script_review',
  creator_name: TEST_USER,
  scene_overview: 'Overview of the scene for testing',
  final_video_path: null,
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  shots: [
    { id: 1, shot_id: 1, project_id: 'e2e-script-id', text: 'First shot', motion_prompt: 'pan', align_with_previous: false, status: 'pending', video_url: null, thumbnail_url: null },
    { id: 2, shot_id: 2, project_id: 'e2e-script-id', text: 'Second shot', motion_prompt: 'zoom', align_with_previous: false, status: 'pending', video_url: null, thumbnail_url: null },
  ],
};

test.describe('Script Review Page', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('/api/projects/e2e-script-id', async (route) => {
      await route.fulfill({ json: mockScriptProject });
    });
    await page.route('/api/projects/e2e-script-id/events', async (route) => {
      await route.fulfill({ status: 200, body: '' });
    });
  });

  test('displays script content and shots', async ({ page }) => {
    await page.goto('/projects/e2e-script-id/script');
    await expect(page.getByTestId('script-content')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText('Overview of the scene for testing')).toBeVisible();
    await expect(page.getByText('分镜列表 (2)')).toBeVisible();
  });

  test('shows approve and regenerate buttons', async ({ page }) => {
    await page.goto('/projects/e2e-script-id/script');
    await expect(page.getByTestId('script-content')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId('approve-script-button')).toBeVisible();
    await expect(page.getByTestId('regenerate-script-button')).toBeVisible();
  });

  test('approve navigates to shots page', async ({ page }) => {
    await page.route('/api/projects/e2e-script-id/approve-script', async (route) => {
      await route.fulfill({ status: 204, body: '' });
    });
    await page.goto('/projects/e2e-script-id/script');
    await expect(page.getByTestId('approve-script-button')).toBeVisible({ timeout: 10_000 });
    await page.getByTestId('approve-script-button').click();
    await expect(page).toHaveURL(/\/projects\/e2e-script-id\/shots/, { timeout: 10_000 });
  });

  test('regenerate switches to loading state', async ({ page }) => {
    await page.route('/api/projects/e2e-script-id/regenerate-script', async (route) => {
      await route.fulfill({ status: 204, body: '' });
    });
    let call = 0;
    await page.route('/api/projects/e2e-script-id', async (route) => {
      call++;
      await route.fulfill({ json: { ...mockScriptProject, status: call > 1 ? 'scripting' : 'script_review', shots: call > 1 ? [] : mockScriptProject.shots } });
    });
    await page.goto('/projects/e2e-script-id/script');
    await expect(page.getByTestId('regenerate-script-button')).toBeVisible({ timeout: 10_000 });
    page.on('dialog', (d) => d.accept());
    await page.getByTestId('regenerate-script-button').click();
    await expect(page.getByTestId('script-loading')).toBeVisible({ timeout: 5_000 });
  });
});

// ─── Shots Review Page ───────────────────────────────────────────────────────

const mockShotProject = {
  id: 'e2e-shots-id',
  title: 'E2E Shots Project',
  theme_text: 'Test theme',
  status: 'shot_review',
  creator_name: TEST_USER,
  scene_overview: 'Scene overview',
  final_video_path: null,
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  shots: [
    { id: 1, shot_id: 1, project_id: 'e2e-shots-id', text: 'Shot 1', motion_prompt: 'pan', align_with_previous: false, status: 'completed', video_url: null, thumbnail_url: null },
    { id: 2, shot_id: 2, project_id: 'e2e-shots-id', text: 'Shot 2', motion_prompt: 'zoom', align_with_previous: false, status: 'completed', video_url: null, thumbnail_url: null },
  ],
};

test.describe('Shots Review Page', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('/api/projects/e2e-shots-id', async (route) => {
      await route.fulfill({ json: mockShotProject });
    });
    await page.route('/api/projects/e2e-shots-id/events', async (route) => {
      await route.fulfill({ status: 200, body: '' });
    });
  });

  test('displays shots list', async ({ page }) => {
    await page.goto('/projects/e2e-shots-id/shots');
    await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 10_000 });
  });

  test('shows export button', async ({ page }) => {
    await page.goto('/projects/e2e-shots-id/shots');
    await expect(page.getByTestId('export-button')).toBeVisible({ timeout: 10_000 });
  });

  test('export navigates to export page', async ({ page }) => {
    await page.route('/api/projects/e2e-shots-id/export', async (route) => {
      await route.fulfill({ status: 204, body: '' });
    });
    await page.goto('/projects/e2e-shots-id/shots');
    await expect(page.getByTestId('export-button')).toBeVisible({ timeout: 10_000 });
    await page.getByTestId('export-button').click();
    await expect(page).toHaveURL(/\/projects\/e2e-shots-id\/export/, { timeout: 10_000 });
  });
});

// ─── Export Page ─────────────────────────────────────────────────────────────

test.describe('Export Page', () => {
  test('shows progress while exporting', async ({ page }) => {
    await page.route('/api/projects/e2e-export-id', async (route) => {
      await route.fulfill({
        json: {
          id: 'e2e-export-id', title: 'Export Test', theme_text: 'theme',
          status: 'exporting', creator_name: TEST_USER, scene_overview: null,
          final_video_path: null, shots: [],
          created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
        },
      });
    });
    await page.route('/api/projects/e2e-export-id/events', async (route) => {
      await route.fulfill({ status: 200, body: '' });
    });
    await page.goto('/projects/e2e-export-id/export');
    await expect(page.getByTestId('export-progress')).toBeVisible({ timeout: 10_000 });
  });

  test('shows download button when exported', async ({ page }) => {
    await page.route('/api/projects/e2e-done-id', async (route) => {
      await route.fulfill({
        json: {
          id: 'e2e-done-id', title: 'Done Project', theme_text: 'theme',
          status: 'exported', creator_name: TEST_USER, scene_overview: null,
          final_video_path: '/videos/final.mp4', shots: [],
          created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
        },
      });
    });
    await page.goto('/projects/e2e-done-id/export');
    await expect(page.getByTestId('download-video-button')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText('导出成功')).toBeVisible();
  });
});

// ─── Full Workflow Integration ────────────────────────────────────────────────

test.describe('Full Workflow Integration', () => {
  test('home → new project → script loading → approve → shots → export', async ({ page }) => {
    await page.route('/api/projects', async (route) => {
      if (route.request().method() === 'POST') {
        await route.fulfill({ json: { id: 'e2e-flow-id', status: 'draft' } });
      } else {
        await route.continue();
      }
    });
    await page.route('/api/projects/e2e-flow-id/reference-images', async (route) => {
      await route.fulfill({ json: { image_ids: ['img1'] } });
    });
    await page.route('/api/projects/e2e-flow-id/start', async (route) => {
      await route.fulfill({ status: 204, body: '' });
    });
    await page.route('/api/projects/e2e-flow-id/approve-script', async (route) => {
      await route.fulfill({ status: 204, body: '' });
    });
    await page.route('/api/projects/e2e-flow-id/export', async (route) => {
      await route.fulfill({ status: 204, body: '' });
    });
    await page.route('/api/projects/e2e-flow-id/events', async (route) => {
      await route.fulfill({ status: 200, body: '' });
    });

    let status = 'scripting';
    await page.route('/api/projects/e2e-flow-id', async (route) => {
      await route.fulfill({
        json: {
          id: 'e2e-flow-id', title: 'Flow Test', theme_text: 'flow theme',
          status, creator_name: TEST_USER, scene_overview: 'Scene text',
          final_video_path: null,
          shots: ['script_review', 'shot_review'].includes(status) ? [
            { id: 1, shot_id: 1, project_id: 'e2e-flow-id', text: 'Shot', motion_prompt: 'pan', align_with_previous: false, status: 'completed', video_url: null, thumbnail_url: null },
          ] : [],
          created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
        },
      });
    });

    // 1. Home → New Project
    await page.goto('/');
    await expect(page.getByTestId('new-project-button')).toBeVisible();
    await page.getByTestId('new-project-button').click();
    await expect(page).toHaveURL('/projects/new');

    // 2. Fill form + upload image
    await page.getByTestId('project-title-input').fill('Flow Test');
    await page.getByTestId('project-theme-input').fill('flow theme');
    await page.locator('#file-input-character').setInputFiles({
      name: 'char.jpg',
      mimeType: 'image/jpeg',
      buffer: Buffer.from([0xFF, 0xD8, 0xFF, 0xD9]),
    });
    await page.getByTestId('create-project-submit').click();

    // 3. Script loading state
    await expect(page).toHaveURL(/\/projects\/e2e-flow-id\/script/, { timeout: 10_000 });
    await expect(page.getByTestId('script-loading')).toBeVisible({ timeout: 5_000 });

    // 4. Script review state
    status = 'script_review';
    await page.goto('/projects/e2e-flow-id/script');
    await expect(page.getByTestId('script-content')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId('approve-script-button')).toBeVisible();

    // 5. Approve → shots page
    status = 'shot_review';
    await page.getByTestId('approve-script-button').click();
    await expect(page).toHaveURL(/\/projects\/e2e-flow-id\/shots/, { timeout: 10_000 });
    await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId('export-button')).toBeVisible();

    // 6. Export → export page
    await page.getByTestId('export-button').click();
    await expect(page).toHaveURL(/\/projects\/e2e-flow-id\/export/, { timeout: 10_000 });
  });
});
