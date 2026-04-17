# Frontend Implementation Design · Video Maker

**Date:** 2026-04-11  
**Status:** Approved  
**Scope:** 框架 + 核心（Scope B）  
**Reference:** [Frontend Architecture](../../frontend/ARCHTECH.md)

---

## 1. 范围说明

**包含：**
- 项目脚手架（`create-next-app` + 依赖安装）
- 所有 lib 层完整实现（types / api / sse / state）
- 4 个共享组件完整实现（UploadZone / ShotCard / ProgressStream / UserBadge）
- 所有页面有实质内容（路由结构、状态逻辑、UI 框架），非空骨架
- Dockerfile + `.env.local`

**不包含：**
- 端到端测试（Playwright）
- 完整的 Vitest 测试套件（仅保留 `computeCascadeWarnings` 纯函数测试示例）
- 生产级 CI 配置

---

## 2. 初始化

```bash
# 在 /home/wayne/tools/video_maker/ 下
npx create-next-app@latest frontend \
  --typescript --tailwind --eslint --app \
  --src-dir=no --import-alias="@/*" --no-git

cd frontend
npm install zustand
npx shadcn@latest init --defaults
npx shadcn@latest add button input textarea dialog badge \
  dropdown-menu tooltip progress switch sonner
```

`.env.local`：
```
NEXT_PUBLIC_API_BASE=http://localhost:8000
```

---

## 3. Lib 层设计

### 3.1 `lib/types.ts`

完整类型定义，与后端 Pydantic schema 对齐：

- `ProjectStatus` — 8 种状态联合类型
- `ShotStatus` — 5 种状态联合类型
- `Project` / `Shot` / `ReferenceImage` / `ProjectDetail`
- `SSEEventType` — 10 种事件类型联合
- `SSEEvent` — 泛型事件包装
- `APIError` / `Toast`（含 `id`、`type`、`message`）

### 3.2 `lib/api.ts`

内部 `request<T>(method, path, body?)` 函数：
- 读 `process.env.NEXT_PUBLIC_API_BASE` 作为 base
- 从 `localStorage` 读 `user_name`，写入 `X-User-Name` header
- `Content-Type: application/json`（非 FormData 时）
- 非 2xx 时解析 `{ error: { code, message } }` 抛出 `Error`

导出方法完全对齐 ARCHTECH.md §6.2（16 个方法 + 2 个 URL 工具函数）。

### 3.3 `lib/sse.ts`

```ts
interface SSEConnection {
  subscribe(event: SSEEventType, handler: (data: unknown) => void): () => void
  close(): void
}
export function createSSEConnection(projectId: string): SSEConnection
```

- URL：`${BASE}/api/projects/${projectId}/events`
- 内部用 `Map<SSEEventType, Set<handler>>` 管理订阅
- `subscribe` 返回 unsubscribe 函数
- 断线重连由 `EventSource` 原生处理

### 3.4 `lib/state.ts`

Zustand store，完整实现 `AppStore`（ARCHTECH.md §5.1）。

额外导出纯函数：
```ts
export function computeCascadeWarnings(
  shots: Shot[],
  selectedIds: Set<number>
): Map<number, number[]>
```
完全按照 ARCHTECH.md §5.4 的算法实现。

---

## 4. 组件设计

### 4.1 UploadZone

Props 完全按 ARCHTECH.md §4.1。行为：
- `dragover` 高亮蓝色边框
- 超出 `maxFiles` 截断，显示 Toast 提示
- 已选图预览（`URL.createObjectURL`）+ 单张删除按钮
- 不负责上传，只管理 `File[]`

### 4.2 ShotCard

Props 完全按 ARCHTECH.md §4.2。三种 variant 渲染内容各自独立（内部 switch）。`review` variant 的运镜提示词编辑用 inline textarea，保存时调用 `onEditPrompt`。

### 4.3 ProgressStream

Props 完全按 ARCHTECH.md §4.3。
- `useEffect` 挂载时建立 SSE，卸载时 close
- 内部用 `useAppStore` 分发所有标准 SSE 事件到 store
- 60 秒无事件超时：显示警告 Toast + 手动拉取项目详情

### 4.4 UserBadge

- 读 `localStorage.user_name` 初始化
- 点击后展开 inline input（shadcn `Input`）
- Enter / blur 保存，同步写 `localStorage` 和 `useAppStore.setUserName`

---

## 5. 页面设计

### 5.1 `app/layout.tsx`

根布局：`Inter` 字体，全局 `<Toaster />`（sonner），metadata。

### 5.2 `app/page.tsx` — 项目列表

- `useState` 管理 projects 数组、搜索关键词、状态筛选、创建者筛选、排序
- `useEffect` 首次加载 + `setInterval(5000)` 轮询 `api.listProjects`
- 顶部工具栏：搜索、状态 select、创建者 select、排序 select、UserBadge、"新建项目"按钮
- 项目卡片网格：标题、创建者、时间、状态 Badge、进度条（处理中）、缩略图（EXPORTED）
- 卡片右上角 DropdownMenu：打开 / 删除（删除需二次确认 Dialog）

### 5.3 `app/projects/new/page.tsx` — 新建项目

- 表单字段：标题（Input）、主题（Textarea）、角色参考图（UploadZone kind=character maxFiles=3）、场景参考图（UploadZone kind=scene maxFiles=3，可选）
- 提交串行三步：createProject → uploadReferenceImages → startPipeline
- 提交期间按钮 loading 禁用，任一步失败 addToast 并停止

### 5.4 `app/projects/[id]/page.tsx` — 状态路由器

`useEffect` 调用 `api.getProject(id)`，按 `status` 做 `router.replace`（完全按 ARCHTECH.md §3.2 映射表）。DRAFT / FAILED 状态停留，FAILED 显示错误 + 重置按钮。

### 5.5 `app/projects/[id]/script/page.tsx` — 脚本审批

- 读 store 中 `currentProject.status`
- **SCRIPTING** 子状态：骨架加载动画 + `<ProgressStream onEvent=...>`，收到 `script_ready` 后 `router.refresh()`
- **SCRIPT_REVIEW** 子状态：`scene_overview` 可编辑 Textarea（blur 时调 `api.patchStoryboard`）、`<ShotCard variant="script" />` 列表、底部 [重新生成脚本][通过] 按钮

### 5.6 `app/projects/[id]/shots/page.tsx` — 分镜视频审批

- **SHOT_GENERATING**：总进度条 + `<ShotCard variant="generating" />` 列表 + `<ProgressStream />`
- **SHOT_REVIEW**：`<ShotCard variant="review" />` 列表、断层警告（`computeCascadeWarnings` 驱动）、底部三个操作按钮
- 断层警告 UI：黄色 Alert，含"一键追加"按钮（调 `toggleShotSelection`）

### 5.7 `app/projects/[id]/export/page.tsx` — 导出

- **EXPORTING**：进度动画 + `<ProgressStream onEvent=...>`，收到 `export_done` 后 `router.refresh()`
- **EXPORTED**：`<video>` 播放器（`api.finalVideoUrl(id)`）、元信息展示、[下载 MP4]（`<a download>`）、[退回分镜审批][退回脚本审批]

---

## 6. Dockerfile

Multi-stage build：
1. `node:20-alpine` deps stage — `npm ci`
2. builder stage — `npm run build`
3. runner stage — 仅复制 `.next/standalone`，端口 3000

---

## 7. 实现顺序（Bottom-up）

1. `create-next-app` 脚手架 + 依赖安装
2. `lib/types.ts`
3. `lib/api.ts`
4. `lib/sse.ts`
5. `lib/state.ts`（含 `computeCascadeWarnings`）
6. `components/UserBadge.tsx`
7. `components/UploadZone.tsx`
8. `components/ProgressStream.tsx`
9. `components/ShotCard.tsx`
10. `app/layout.tsx`
11. `app/page.tsx`（项目列表）
12. `app/projects/new/page.tsx`
13. `app/projects/[id]/page.tsx`（状态路由器）
14. `app/projects/[id]/script/page.tsx`
15. `app/projects/[id]/shots/page.tsx`
16. `app/projects/[id]/export/page.tsx`
17. `Dockerfile` + `.env.local`
