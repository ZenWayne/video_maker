# Frontend Development Rules

## No Hardcoded Absolute Paths

**Never write hardcoded absolute paths in any file (code, config, scripts).**

Use relative paths or dynamic resolution instead:

```typescript
// TypeScript/JavaScript — relative to current file
import path from 'path'
const file = path.resolve(__dirname, '../fixtures/test.jpg')   // ✓
const file = '/home/wayne/tools/video_maker/tests/fixtures/test.jpg'  // ✗

// Vite config — use __dirname
const alias = path.resolve(__dirname, './src')  // ✓
const alias = '/home/wayne/tools/video_maker/frontend-vite/src'  // ✗
```

## API Base URL

**NEVER hardcode `http://localhost:8000` or any full URL for API requests.**

Always use a relative path `/api` so requests go through the Next.js rewrite proxy:

```typescript
// CORRECT
const BASE = process.env.NEXT_PUBLIC_API_BASE || '/api'

// WRONG - don't do this
const BASE = 'http://localhost:8000'
```

### Why?

1. **CORS**: Browser blocks direct localhost requests in production
2. **Proxy**: Next.js rewrites `/api/*` to the backend (configured in `next.config.ts`)
3. **Flexibility**: Works in both dev and production without code changes

### Configuration

Development (next.config.ts):
```typescript
async rewrites() {
  return [
    { source: '/api/:path*', destination: 'http://127.0.0.1:8000/:path*' },
  ]
}
```

Production (nginx):
```nginx
location /api/ {
    proxy_pass http://backend:8000/;
}
```

## Testing with Playwright

**Always add `data-testid` attributes to interactive elements for reliable E2E testing.**

### Naming Convention

```typescript
// Buttons
<Button data-testid="new-project-button">新建项目</Button>
<Button data-testid="create-project-submit">创建</Button>
<Button data-testid="delete-project-button">删除</Button>

// Form inputs
<Input data-testid="project-title-input" />
<Textarea data-testid="project-theme-input" />

// Lists and cards
<div data-testid="project-list">
  <Card data-testid="project-card">...</Card>
</div>

// Status/loading indicators
<div data-testid="loading-spinner" />
<div data-testid="generation-progress" />
```

### Pattern: `[action]-[entity]-[element]`

- **action**: `new`, `create`, `delete`, `edit`, `save`, `cancel`, `search`, `filter`
- **entity**: `project`, `shot`, `script`, `image`, `video`
- **element**: `button`, `input`, `list`, `card`, `modal`, `form`

### Examples

| Element | data-testid |
|---------|-------------|
| 新建项目按钮 | `new-project-button` |
| 项目标题输入框 | `project-title-input` |
| 创建项目提交按钮 | `create-project-submit` |
| 项目列表容器 | `project-list` |
| 项目卡片 | `project-card` |
| 搜索输入框 | `search-input` |
| 状态筛选下拉 | `status-filter` |
| 脚本内容区域 | `script-content` |
| 审批脚本按钮 | `approve-script-button` |
| 分镜列表 | `shots-list` |
| 导出视频按钮 | `export-video-button` |

### Playwright Example

```typescript
// Click new project button
await page.getByTestId('new-project-button').click();

// Fill form
await page.getByTestId('project-title-input').fill('Test Project');
await page.getByTestId('project-theme-input').fill('A story about testing');

// Submit
await page.getByTestId('create-project-submit').click();

// Verify list updated
await expect(page.getByTestId('project-list')).toContainText('Test Project');
```
