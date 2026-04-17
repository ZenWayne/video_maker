# Playwright 端到端测试计划

## 概述

本文档按功能模块描述 Video Maker 的 Playwright E2E 测试计划。测试覆盖完整的视频生成工作流：**新建项目 → 脚本生成 → 脚本审批 → 分镜生成 → 分镜审批 → 导出**。

状态机流转：
```
DRAFT → SCRIPTING → SCRIPT_REVIEW → SHOT_GENERATING → SHOT_REVIEW → EXPORTING → EXPORTED
         ↓                ↓                ↓               ↓             ↓
        FAILED           FAILED           FAILED          FAILED        FAILED
                                                                          ↓
                                                                        DRAFT
```

---

## 前置约定

| 符号 | 含义 |
|------|------|
| `[PRE]` | 该 subplan 依赖的前置条件或前置 subplan |
| `[PARALLEL]` | 可与其他 subplan 并行执行 |
| `[SERIAL]` | 必须串行执行，不可并行 |

**全局前置**：所有测试需要：
- 后端服务运行（FastAPI + Redis + Worker）
- 前端服务运行（Vite dev server 或 production build）
- 设置 `X-User-Name` header（通过 UserBadge 组件输入用户名）

---

## 模块 1：首页（项目列表）

### Subplan 1.1 — 项目列表展示 `[PARALLEL]`

**前置**：数据库中存在若干项目（不同状态）

| # | 测试用例 | 验证点 |
|---|----------|--------|
| 1.1.1 | 访问首页，展示项目列表 | `[data-testid="project-list"]` 可见，卡片数量与 API 返回一致 |
| 1.1.2 | 每张卡片显示标题、状态徽章、创建者、日期 | Badge 颜色与状态对应（draft=灰、exported=绿、failed=红） |
| 1.1.3 | `exported` 状态卡片展示视频缩略图 | `<video>` 元素存在 |
| 1.1.4 | `scripting/shot_generating/exporting` 状态展示进度流 | `ProgressStream` 组件可见 |
| 1.1.5 | 页面每 5 秒自动轮询刷新 | mock 定时器验证 `api.listProjects` 被重复调用 |

---

### Subplan 1.2 — 搜索与过滤 `[PARALLEL]`

**前置**：Subplan 1.1 通过（有项目数据）

| # | 测试用例 | 验证点 |
|---|----------|--------|
| 1.2.1 | 搜索框输入标题关键词，列表实时过滤 | 不匹配卡片消失 |
| 1.2.2 | 状态下拉选择 `draft`，仅显示草稿项目 | `[data-testid="status-filter"]` 触发 API `?status=draft` |
| 1.2.3 | 清空搜索词，列表恢复全部 | 卡片数量还原 |
| 1.2.4 | 无匹配结果时展示空状态 | 「暂无项目」文案和「创建第一个项目」按钮可见 |

---

### Subplan 1.3 — 项目操作（删除/打开） `[PARALLEL]`

**前置**：Subplan 1.1 通过

| # | 测试用例 | 验证点 |
|---|----------|--------|
| 1.3.1 | 点击卡片正文，跳转到项目详情页 | URL 变为 `/projects/{id}` |
| 1.3.2 | 下拉菜单「打开」按钮，跳转项目页 | 同上 |
| 1.3.3 | 下拉菜单「删除」→ 确认弹窗 → 删除成功 | 卡片从列表移除，Toast 显示「项目已删除」 |
| 1.3.4 | 删除弹窗点击「取消」，项目保留 | 卡片仍在列表中 |
| 1.3.5 | 删除不存在的项目（404），展示错误 Toast | Toast type=error |

---

### Subplan 1.4 — 用户名设置 `[PARALLEL]`

**前置**：无（独立功能）

| # | 测试用例 | 验证点 |
|---|----------|--------|
| 1.4.1 | 未设置用户名时，展示 info Toast 提示 | 「请先点击右上角设置用户名」 |
| 1.4.2 | UserBadge 输入用户名并保存，Header 更新 | LocalStorage 写入 `userName` |
| 1.4.3 | 刷新页面，用户名持久化 | UserBadge 显示已保存的用户名 |

---

## 模块 2：新建项目

### Subplan 2.1 — 表单校验 `[PARALLEL]`

**前置**：用户名已设置（Subplan 1.4）

| # | 测试用例 | 验证点 |
|---|----------|--------|
| 2.1.1 | 标题为空提交，显示错误 Toast | 「请输入项目标题」 |
| 2.1.2 | 主题描述为空提交，显示错误 Toast | 「请输入主题描述」 |
| 2.1.3 | 未上传角色图提交，显示错误 Toast | 「请上传至少一张角色参考图」 |
| 2.1.4 | 三项都填写后，提交按钮可点击，无 Toast 拦截 | 请求被发出 |

---

### Subplan 2.2 — 图片上传 `[PARALLEL]`

**前置**：页面打开（不依赖其他 subplan）

| # | 测试用例 | 验证点 |
|---|----------|--------|
| 2.2.1 | 拖拽单张图片到角色上传区，预览显示 | UploadZone 内图片缩略图可见 |
| 2.2.2 | 点击上传区选择多张图片，全部预览 | 多个缩略图 |
| 2.2.3 | 上传场景参考图（可选），显示预览 | 场景上传区展示图片 |
| 2.2.4 | 上传非图片文件，显示错误 | 错误 Toast 或上传区提示 |
| 2.2.5 | 删除已上传图片，预览消失 | 缩略图移除 |

---

### Subplan 2.3 — 完整创建流程 `[SERIAL]`

**前置**：Subplan 2.1 + 2.2 通过（依赖表单校验和上传功能正常）

> **注意**：此 subplan 会调用真实 API，需隔离测试数据或使用 mock

| # | 测试用例 | 验证点 |
|---|----------|--------|
| 2.3.1 | 填写标题+主题+上传角色图，点击提交 | Loading spinner 显示 |
| 2.3.2 | API 创建项目成功 (`POST /api/projects`) | 返回 `project_id` |
| 2.3.3 | API 上传角色图成功 (`POST /api/projects/{id}/reference-images`) | 文件写入存储 |
| 2.3.4 | API 启动 pipeline 成功 (`POST /api/projects/{id}/start`) | 状态变为 `scripting` |
| 2.3.5 | 成功后跳转到脚本页 `/projects/{id}/script` | URL 正确 |
| 2.3.6 | 创建失败（服务异常），显示错误 Toast，页面不跳转 | 按钮恢复可用 |

---

## 模块 3：脚本审批页

### Subplan 3.1 — 脚本生成中状态 `[SERIAL]`

**前置**：`[PRE]` Subplan 2.3（项目处于 `scripting` 状态）

| # | 测试用例 | 验证点 |
|---|----------|--------|
| 3.1.1 | 进入脚本页，显示「脚本生成中」状态 | `ProgressStream` 可见，操作按钮禁用 |
| 3.1.2 | SSE 流接收进度事件，进度条更新 | 进度文本实时变化 |
| 3.1.3 | 状态轮询检测到 `script_review`，自动刷新内容 | 分镜卡片渲染 |

---

### Subplan 3.2 — 脚本内容查看 `[SERIAL]`

**前置**：`[PRE]` Subplan 3.1（项目处于 `script_review` 状态）

| # | 测试用例 | 验证点 |
|---|----------|--------|
| 3.2.1 | 展示 scene_overview 文本区域 | 内容与 API 返回一致 |
| 3.2.2 | 展示所有 Shot 卡片（ShotCard 组件） | 卡片数量与 shots 数组一致 |
| 3.2.3 | 每张卡片显示：shot_id、文本、镜头类型、视觉描述、时长 | 字段正确 |
| 3.2.4 | 带 word_count_warning 的 Shot 显示警告标识 | 警告 UI 可见 |

---

### Subplan 3.3 — 脚本编辑 `[SERIAL]`

**前置**：`[PRE]` Subplan 3.2（`script_review` 状态）

| # | 测试用例 | 验证点 |
|---|----------|--------|
| 3.3.1 | 修改 scene_overview，保存成功 | `PATCH /api/projects/{id}/storyboard` 被调用 |
| 3.3.2 | 修改 Shot 文本/镜头类型，保存更新 | 本地状态更新，API 调用成功 |
| 3.3.3 | 非 `script_review` 状态进入编辑，返回 409，Toast 报错 | 「Project must be in script_review」 |

---

### Subplan 3.4 — 脚本操作按钮 `[SERIAL]`

**前置**：`[PRE]` Subplan 3.2

| # | 测试用例 | 验证点 |
|---|----------|--------|
| 3.4.1 | 点击「重新生成脚本」→ 确认 → API 调用 | `POST /api/projects/{id}/regenerate-script`，状态回到 `scripting` |
| 3.4.2 | 点击「审批通过」→ API 调用 → 跳转分镜页 | `POST /api/projects/{id}/approve-script`，跳转 `/projects/{id}/shots` |
| 3.4.3 | 审批通过前未保存的编辑，提示确认或自动保存 | 确保数据不丢失 |

---

## 模块 4：分镜审批页

### Subplan 4.1 — 分镜生成中状态 `[SERIAL]`

**前置**：`[PRE]` Subplan 3.4（项目处于 `shot_generating` 状态）

| # | 测试用例 | 验证点 |
|---|----------|--------|
| 4.1.1 | 进入分镜页，展示生成进度 | `ProgressStream` 可见 |
| 4.1.2 | 各 Shot 显示各自状态（pending/prompt_generating/video_generating） | 状态徽章实时更新 |
| 4.1.3 | 状态轮询检测到 `shot_review`，刷新页面 | 操作按钮出现 |

---

### Subplan 4.2 — 分镜内容查看 `[SERIAL]`

**前置**：`[PRE]` Subplan 4.1（项目处于 `shot_review` 状态，所有 shot = completed）

| # | 测试用例 | 验证点 |
|---|----------|--------|
| 4.2.1 | 展示所有已完成分镜的视频 | `<video>` 播放器可见，有 video_path |
| 4.2.2 | 展示分镜的 first_frame 截图 | 图片加载成功 |
| 4.2.3 | failed shot 显示错误信息和重试按钮 | `error_message` 文本可见 |

---

### Subplan 4.3 — 分镜选择与批量重新生成 `[SERIAL]`

**前置**：`[PRE]` Subplan 4.2（`shot_review` 状态）

| # | 测试用例 | 验证点 |
|---|----------|--------|
| 4.3.1 | 勾选多个 Shot，「重新生成选中」按钮激活 | 按钮从禁用变可用 |
| 4.3.2 | 勾选带 `align_with_previous=true` 的下游 Shot，显示级联警告 | `computeCascadeWarnings` 计算结果渲染 |
| 4.3.3 | 点击「重新生成选中」→ API 调用 | `POST /api/projects/{id}/regenerate-shots` 传入正确 shot_ids |
| 4.3.4 | 重新生成后状态回到 `shot_generating` | 进度流重新显示 |

---

### Subplan 4.4 — 分镜编辑 `[SERIAL]`

**前置**：`[PRE]` Subplan 4.2（`shot_review` 状态）

| # | 测试用例 | 验证点 |
|---|----------|--------|
| 4.4.1 | 修改 motion_prompt，保存 | `PATCH /api/projects/{id}/shots/{shot_id}` 调用成功 |
| 4.4.2 | 修改 align_with_previous toggle，保存 | 数据库记录更新 |
| 4.4.3 | 点击「返回脚本」→ 确认 → 状态回到 scripting | `POST /api/projects/{id}/reset-to-script` |

---

### Subplan 4.5 — 导出触发 `[SERIAL]`

**前置**：`[PRE]` Subplan 4.2（所有 Shot 状态均为 `completed`）

| # | 测试用例 | 验证点 |
|---|----------|--------|
| 4.5.1 | 所有分镜完成，「导出视频」按钮激活 | 按钮可点击 |
| 4.5.2 | 存在未完成分镜，「导出视频」按钮禁用或提示 | 错误提示 |
| 4.5.3 | 点击「导出视频」→ API 调用 → 跳转导出页 | `POST /api/projects/{id}/export`，URL 变为 `/projects/{id}/export` |

---

## 模块 5：导出页

### Subplan 5.1 — 导出中状态 `[SERIAL]`

**前置**：`[PRE]` Subplan 4.5（项目处于 `exporting` 状态）

| # | 测试用例 | 验证点 |
|---|----------|--------|
| 5.1.1 | 进入导出页，显示导出进度 | `ProgressStream` 可见，状态 Badge = 「导出中」 |
| 5.1.2 | 状态轮询检测到 `exported`，刷新页面 | 下载按钮出现 |

---

### Subplan 5.2 — 下载与回退 `[SERIAL]`

**前置**：`[PRE]` Subplan 5.1（项目处于 `exported` 状态）

| # | 测试用例 | 验证点 |
|---|----------|--------|
| 5.2.1 | 展示最终视频播放器 | `<video>` 可播放 |
| 5.2.2 | 点击「下载」按钮，触发 `GET /api/projects/{id}/final.mp4` | 浏览器下载对话框或文件下载 |
| 5.2.3 | 点击「重新生成分镜」→ 跳转分镜页 | `POST /api/projects/{id}/regenerate-shots` 或回到 shot_review |
| 5.2.4 | 点击「重新生成脚本」→ 跳转脚本页 | 状态回到 scripting |

---

## 模块 6：错误处理与边界条件

### Subplan 6.1 — API 错误处理 `[PARALLEL]`

**前置**：无（使用 mock 或 route intercept）

| # | 测试用例 | 验证点 |
|---|----------|--------|
| 6.1.1 | 网络断开时请求失败，Toast type=error | 不崩溃，有友好提示 |
| 6.1.2 | 状态转换非法（409），Toast 显示 conflict 信息 | `InvalidTransitionError` 消息展示 |
| 6.1.3 | 访问不存在项目（404），展示错误状态 | 页面不白屏 |
| 6.1.4 | 服务端 500，Toast type=error | 通用错误消息 |

---

### Subplan 6.2 — FAILED 状态处理 `[PARALLEL]`

**前置**：构造一个 `failed` 状态项目

| # | 测试用例 | 验证点 |
|---|----------|--------|
| 6.2.1 | 首页显示 failed 项目，红色 Badge | 卡片 Badge = 「失败」 |
| 6.2.2 | 进入 failed 项目，显示 error_message | 错误文案可见 |
| 6.2.3 | 点击「重置为草稿」→ API 调用 → 状态回 draft | `POST /api/projects/{id}/reset` |
| 6.2.4 | 重置后可重新发起创建流程 | 「启动」按钮可用 |

---

## 并行执行策略

下图展示各 subplan 的依赖关系与并行执行机会：

```
独立并行组 A（无依赖，可完全并行）：
  ├── 1.1 项目列表展示
  ├── 1.2 搜索过滤
  ├── 1.3 项目操作
  ├── 1.4 用户名设置
  ├── 2.1 表单校验
  ├── 2.2 图片上传
  ├── 6.1 API 错误处理
  └── 6.2 FAILED 状态处理

串行主流程（必须按顺序）：
  2.3 → 3.1 → 3.2 → 3.3 / 3.4(并行)
                            ↓
                          4.1 → 4.2 → 4.3 / 4.4 / 4.5(并行)
                                                   ↓
                                                 5.1 → 5.2
```

### Playwright 项目配置建议

```typescript
// playwright.config.ts
export default defineConfig({
  projects: [
    // 组 A：完全并行，不依赖数据库状态
    {
      name: 'unit-ui',
      testMatch: [
        '**/1.1-*.spec.ts',
        '**/1.2-*.spec.ts',
        '**/1.3-*.spec.ts',
        '**/1.4-*.spec.ts',
        '**/2.1-*.spec.ts',
        '**/2.2-*.spec.ts',
        '**/6.1-*.spec.ts',
        '**/6.2-*.spec.ts',
      ],
      use: { ...devices['Desktop Chrome'] },
    },
    // 组 B：串行主流程（用 test.describe.serial 保证顺序）
    {
      name: 'e2e-pipeline',
      testMatch: ['**/pipeline-flow.spec.ts'],
      workers: 1, // 串行执行
    },
  ],
  fullyParallel: true, // 组 A 内部并行
})
```

---

## 测试数据策略

| 场景 | 策略 |
|------|------|
| 独立 UI 测试（组 A） | 使用 `page.route()` mock API 响应，不依赖真实服务 |
| 串行主流程（组 B） | 每次测试前执行 `beforeEach` 清理数据库，创建干净项目 |
| 长时任务（脚本生成/视频生成） | Mock SSE 流 + 预置 `script_review` / `shot_review` 状态的 fixture |
| 文件上传 | 准备测试用小图片资源 `fixtures/test-character.jpg` |

---

## 覆盖率目标

| 模块 | 用例数 | 优先级 |
|------|--------|--------|
| 首页 | 17 | P1 |
| 新建项目 | 13 | P0 |
| 脚本审批 | 13 | P0 |
| 分镜审批 | 17 | P0 |
| 导出 | 7 | P1 |
| 错误处理 | 8 | P1 |
| **合计** | **75** | — |
