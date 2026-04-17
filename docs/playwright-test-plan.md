# Playwright 主流程端到端测试计划

## 状态机主路径

```
DRAFT → SCRIPTING → SCRIPT_REVIEW → SHOT_GENERATING → SHOT_REVIEW → EXPORTING → EXPORTED
```

## 前置约定

| 符号 | 含义 |
|------|------|
| `[PRE]` | 依赖的前置 subplan，必须先完成 |
| `[PARALLEL]` | 可与同层其他 subplan 并行执行 |
| `[SERIAL]` | 必须串行，依赖上一步产生的状态 |

**全局前置**：后端（FastAPI + Redis + Worker）和前端均已启动，用户名已通过 UserBadge 设置。

---

## Subplan 1 — 新建项目 `[SERIAL]`

**前置**：无

| # | 操作 | 验证点 |
|---|------|--------|
| 1.1 | 点击首页「新建项目」按钮 | 跳转 `/projects/new` |
| 1.2 | 填写项目标题和主题描述 | 输入框正常响应 |
| 1.3 | 上传至少一张角色参考图 | UploadZone 显示缩略图预览 |
| 1.4 | 点击提交，等待 Loading | `POST /api/projects` → `POST /api/projects/{id}/reference-images` → `POST /api/projects/{id}/start` 依次调用成功 |
| 1.5 | 成功后自动跳转脚本页 | URL 变为 `/projects/{id}/script`，项目状态为 `scripting` |

---

## Subplan 2 — 脚本生成等待 `[SERIAL]`

**前置**：`[PRE]` Subplan 1（项目处于 `scripting` 状态）

| # | 操作 | 验证点 |
|---|------|--------|
| 2.1 | 进入脚本页，展示生成中状态 | `ProgressStream` 组件可见，审批按钮禁用 |
| 2.2 | SSE 流接收进度事件 | 进度文本实时更新 |
| 2.3 | Worker 完成，状态变为 `script_review` | 页面自动刷新，Shot 卡片列表出现，操作按钮激活 |

---

## Subplan 3 — 脚本审批 `[SERIAL]`

**前置**：`[PRE]` Subplan 2（项目处于 `script_review` 状态）

以下两个 subplan 共享同一个 `script_review` fixture，可**并行**执行：

### Subplan 3a — 查看与编辑脚本 `[PARALLEL]`

| # | 操作 | 验证点 |
|---|------|--------|
| 3a.1 | 展示 scene_overview 文本区域 | 内容与 API 返回一致 |
| 3a.2 | 展示所有 Shot 卡片（shot_id、文本、镜头类型、时长） | 字段正确渲染 |
| 3a.3 | 修改 scene_overview 并保存 | `PATCH /api/projects/{id}/storyboard` 调用成功 |
| 3a.4 | 修改某个 Shot 的文本并保存 | 本地状态同步更新 |

### Subplan 3b — 审批通过 `[PARALLEL]`

| # | 操作 | 验证点 |
|---|------|--------|
| 3b.1 | 点击「审批通过」按钮 | `POST /api/projects/{id}/approve-script` 调用，状态变为 `shot_generating` |
| 3b.2 | 自动跳转分镜页 | URL 变为 `/projects/{id}/shots` |

> **注意**：3b 会改变项目状态，执行时需与 3a 使用**独立的项目 fixture**，避免互相干扰。

---

## Subplan 4 — 分镜生成等待 `[SERIAL]`

**前置**：`[PRE]` Subplan 3b（项目处于 `shot_generating` 状态）

| # | 操作 | 验证点 |
|---|------|--------|
| 4.1 | 进入分镜页，展示生成进度 | `ProgressStream` 可见 |
| 4.2 | 各 Shot 状态实时更新（`pending` → `prompt_generating` → `video_generating`） | 状态徽章变化 |
| 4.3 | Worker 完成，所有 Shot 变为 `completed` | 页面自动刷新，视频播放器出现，导出按钮激活 |

---

## Subplan 5 — 分镜审批 `[SERIAL]`

**前置**：`[PRE]` Subplan 4（项目处于 `shot_review` 状态，所有 shot = `completed`）

以下三个 subplan 共享同一个 `shot_review` fixture，可**并行**执行：

### Subplan 5a — 查看分镜 `[PARALLEL]`

| # | 操作 | 验证点 |
|---|------|--------|
| 5a.1 | 展示每个 Shot 的视频播放器 | `<video>` 元素可见，`video_path` 有效 |
| 5a.2 | 展示 first_frame 首帧截图 | 图片加载成功 |

### Subplan 5b — 重新生成指定分镜 `[PARALLEL]`

| # | 操作 | 验证点 |
|---|------|--------|
| 5b.1 | 勾选若干 Shot | 「重新生成选中」按钮从禁用变可用 |
| 5b.2 | 勾选含 `align_with_previous=true` 的下游 Shot | 级联警告 UI 出现（`computeCascadeWarnings` 渲染结果） |
| 5b.3 | 点击「重新生成选中」 | `POST /api/projects/{id}/regenerate-shots` 传入正确 `shot_ids`，状态回到 `shot_generating` |

### Subplan 5c — 触发导出 `[PARALLEL]`

| # | 操作 | 验证点 |
|---|------|--------|
| 5c.1 | 所有分镜已完成，「导出视频」按钮可点击 | 按钮未禁用 |
| 5c.2 | 点击「导出视频」 | `POST /api/projects/{id}/export` 调用，状态变为 `exporting` |
| 5c.3 | 自动跳转导出页 | URL 变为 `/projects/{id}/export` |

> **注意**：5c 会改变项目状态，需与 5a/5b 使用**独立的项目 fixture**。

---

## Subplan 6 — 导出完成与下载 `[SERIAL]`

**前置**：`[PRE]` Subplan 5c（项目处于 `exporting` 状态）

| # | 操作 | 验证点 |
|---|------|--------|
| 6.1 | 进入导出页，展示导出进度 | `ProgressStream` 可见，状态 Badge = 「导出中」 |
| 6.2 | Worker 完成，状态变为 `exported` | 页面自动刷新，最终视频播放器和下载按钮出现 |
| 6.3 | 点击「下载」按钮 | 触发 `GET /api/projects/{id}/final.mp4`，浏览器开始下载 |

---

## 执行顺序总览

```
Subplan 1 (新建项目)
    ↓
Subplan 2 (脚本生成等待)
    ↓
┌───────────────┬──────────────────┐
Subplan 3a      Subplan 3b        ← 并行（独立 fixture）
(查看/编辑脚本)  (审批通过)
                ↓
            Subplan 4 (分镜生成等待)
                ↓
┌──────────┬──────────────────┬──────────┐
Subplan 5a  Subplan 5b        Subplan 5c ← 并行（独立 fixture）
(查看分镜)  (重新生成分镜)     (触发导出)
                               ↓
                           Subplan 6 (导出完成与下载)
```

---

## 测试数据策略

| 阶段 | 策略 |
|------|------|
| Subplan 1 | 调用真实 API，`afterEach` 清理创建的项目 |
| Subplan 2–6 串行主路径 | 通过 API 直接预置各状态的项目作为 fixture，跳过耗时的 AI 生成等待 |
| 并行 subplan（3a/3b, 5a/5b/5c） | 每个并行分支通过 fixture factory 独立创建同状态的项目，互不干扰 |
| 文件上传 | 准备固定测试图片 `fixtures/test-character.jpg`（小尺寸，快速上传） |
