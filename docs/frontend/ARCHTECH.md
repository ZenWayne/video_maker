# Frontend Architecture — Video Maker

> 面向实现的开发者参考文档。基于 [设计规范](../superpowers/specs/2026-04-11-video-maker-agent-design.md)。

---

## 目录

1. [技术栈](#1-技术栈)
2. [目录结构](#2-目录结构)
3. [路由与页面](#3-路由与页面)
4. [组件架构](#4-组件架构)
5. [状态管理](#5-状态管理)
6. [API 客户端层](#6-api-客户端层)
7. [SSE 实时推送](#7-sse-实时推送)
8. [TypeScript 类型定义](#8-typescript-类型定义)
9. [UI 设计系统](#9-ui-设计系统)
10. [错误处理](#10-错误处理)
11. [测试策略](#11-测试策略)

---

## 1. 技术栈

| 分类 | 选型 | 说明 |
|---|---|---|
| 框架 | Next.js 14 (App Router) | 服务端组件 + 客户端组件混合 |
| 语言 | TypeScript | 严格模式 |
| 样式 | Tailwind CSS | 原子类 |
| 组件库 | shadcn/ui | 基于 Radix UI，按需拷贝组件 |
| 状态管理 | Zustand | 全局轻量状态 |
| HTTP 客户端 | 原生 `fetch` | 封装在 `lib/api.ts` |
| 实时推送 | 浏览器原生 `EventSource` | 封装在 `lib/sse.ts` |
| 视频 / 图片 | 原生 `<video>` + shadcn Dialog | 无额外依赖 |
| 测试 | Vitest + React Testing Library | 组件单元测试 |

---

## 2. 目录结构

```
frontend/
├── app/                          # Next.js App Router
│   ├── layout.tsx                # 根布局：字体、全局 Toast Provider
│   ├── page.tsx                  # 首页 / 项目列表
│   └── projects/
│       ├── new/
│       │   └── page.tsx          # 新建项目
│       └── [id]/
│           ├── page.tsx          # 项目入口（智能跳转）
│           ├── script/
│           │   └── page.tsx      # 脚本审批页
│           ├── shots/
│           │   └── page.tsx      # 分镜视频审批页
│           └── export/
│               └── page.tsx      # 导出页
│
├── components/
│   ├── UploadZone.tsx            # 参考图拖拽上传
│   ├── ShotCard.tsx              # 单镜卡片
│   ├── ProgressStream.tsx        # SSE 进度订阅 + 进度条
│   └── UserBadge.tsx             # 用户名显示 / 修改
│
├── lib/
│   ├── api.ts                    # 后端 REST API 封装
│   ├── sse.ts                    # EventSource 封装
│   ├── state.ts                  # Zustand store
│   └── types.ts                  # 全局 TypeScript 类型
│
├── public/                       # 静态资源
├── package.json
├── tailwind.config.ts
├── tsconfig.json
└── Dockerfile
```

---

## 3. 路由与页面

### 3.1 路由结构

| 路由 | 页面文件 | 触发条件 |
|---|---|---|
| `/` | `app/page.tsx` | 项目列表首页 |
| `/projects/new` | `app/projects/new/page.tsx` | 新建项目 |
| `/projects/[id]` | `app/projects/[id]/page.tsx` | 项目入口，自动跳转 |
| `/projects/[id]/script` | `app/projects/[id]/script/page.tsx` | 脚本审批 |
| `/projects/[id]/shots` | `app/projects/[id]/shots/page.tsx` | 分镜视频审批 |
| `/projects/[id]/export` | `app/projects/[id]/export/page.tsx` | 导出 |

### 3.2 项目入口的跳转逻辑

`/projects/[id]/page.tsx` 是一个状态路由器，读取项目当前状态后做客户端跳转：

```
project.status
  DRAFT              → 停留，显示"开始生成"按钮
  SCRIPTING          → redirect /projects/[id]/script
  SCRIPT_REVIEW      → redirect /projects/[id]/script
  SHOT_GENERATING    → redirect /projects/[id]/shots
  SHOT_REVIEW        → redirect /projects/[id]/shots
  EXPORTING          → redirect /projects/[id]/export
  EXPORTED           → redirect /projects/[id]/export
  FAILED             → 停留，显示错误详情 + "重置"按钮
```

实现方式：`useEffect` 中调用 `GET /api/projects/{id}`，根据 `status` 字段调用 `router.replace()`。

### 3.3 各页面职责

#### `/` — 项目列表

- 展示所有项目的卡片网格
- 顶部：搜索框（title 模糊匹配）+ 状态筛选 + 创建者筛选 + 排序
- 右上角：`<UserBadge />` + "新建项目"按钮
- 每张卡片：标题、创建者、创建时间、状态徽章、成片缩略图（EXPORTED 状态）、进度条（处理中）
- 卡片右上角菜单：打开 / 删除
- **刷新策略**：`setInterval` 每 5 秒轮询一次 `GET /api/projects`

#### `/projects/new` — 新建项目

表单字段：项目标题、主题（一句话）、角色参考图（必填，1-3 张）、场景参考图（可选，1-3 张）

提交流程（串行三步）：
```
POST /api/projects                        → 拿到 project_id
POST /api/projects/{id}/reference-images  → 上传每批图
POST /api/projects/{id}/start             → 启动 pipeline
router.push(`/projects/${id}/script`)
```

#### `/projects/[id]/script` — 脚本审批

两种子状态：

- **SCRIPTING**：加载动画 + `<ProgressStream />`，监听 SSE `script_ready` 事件后刷新页面
- **SCRIPT_REVIEW**：
  - 顶部 `scene_overview` 可编辑文本区
  - 分镜卡片列表（使用 `<ShotCard variant="script" />`）
  - 每卡含：台词、景别、时长、字数警告徽章、对齐开关（`🔗 / ✂`）、[编辑] 按钮
  - 底部：[重新生成脚本] [通过，开始生成视频 →]

#### `/projects/[id]/shots` — 分镜视频审批

两种子状态：

- **SHOT_GENERATING**：总体进度条、每镜子状态卡片（`pending / prompt_generating / video_generating / completed / failed`），SSE 驱动实时更新
- **SHOT_REVIEW**：
  - 每镜卡片（`<ShotCard variant="review" />`）：视频播放器、对齐标签、尾帧缩略图、运镜提示词（可编辑）、多选框
  - 智能断层提示（见 [5.4 断层警告逻辑](#54-断层警告逻辑)）
  - 底部：[退回修改脚本] [重跑选中的镜] [全部通过，导出 →]
  - 有 `FAILED` 状态的 shot 时，"全部通过"按钮禁用

#### `/projects/[id]/export` — 导出

- **EXPORTING**：进度动画 + `<ProgressStream />`，监听 SSE `export_done`
- **EXPORTED**：视频播放器、元信息、[下载 MP4] 主按钮、[退回分镜审批] [退回脚本审批] 次级入口

---

## 4. 组件架构

### 4.1 UploadZone

**职责**：参考图多文件拖拽上传，支持点击选择。

**Props**：
```ts
interface UploadZoneProps {
  kind: 'character' | 'scene'
  maxFiles: number           // 1-3
  value: File[]
  onChange: (files: File[]) => void
}
```

**行为**：
- 接受 `image/*` 类型
- 拖拽进入时高亮边框
- 超出 `maxFiles` 时截断并提示
- 展示已选图片缩略图 + 删除按钮
- 不负责上传（上传由页面在提交时统一处理）

### 4.2 ShotCard

**职责**：展示单个 shot 的信息，在脚本审批和视频审批两个场景复用，通过 `variant` prop 切换。

**Props**：
```ts
interface ShotCardProps {
  shot: Shot
  variant: 'script' | 'review' | 'generating'
  selected?: boolean                              // 多选框状态（review）
  onSelect?: (shotId: number) => void             // 勾选重跑（review）
  onEditScript?: (shotId: number) => void         // 编辑脚本（script）
  onEditPrompt?: (shotId: number, prompt: string) => void  // 编辑提示词（review）
  onViewFirstFrame?: (shotId: number) => void     // 查看首帧（review）
}
```

**各 variant 渲染内容**：

| variant | 展示内容 |
|---|---|
| `script` | shot_id、shot_type、shot_duration、text、字数警告徽章、对齐开关、[编辑]按钮 |
| `generating` | shot_id、子状态 badge（pending/generating/completed/failed）、进度 spinner |
| `review` | 视频播放器、对齐标签、尾帧缩略图、motion_prompt（可编辑内联或 modal）、多选框、[查看首帧][下载] |

### 4.3 ProgressStream

**职责**：订阅 SSE，展示当前 pipeline 进度，并在特定事件发生时通知父组件刷新。

**Props**：
```ts
interface ProgressStreamProps {
  projectId: string
  onEvent?: (event: SSEEvent) => void   // 父组件监听特定事件（如 script_ready）
}
```

**行为**：
- 挂载时建立 SSE 连接（调用 `lib/sse.ts`）
- 卸载时关闭连接
- 根据 `state_snapshot` 初始化进度状态
- 监听 `shot_started` / `shot_completed` / `shot_failed` 更新各镜进度
- SSE 断线自动重连（EventSource 原生行为），重连后拉取一次项目详情同步状态
- 60 秒无事件时显示"检查服务器状态..."并手动拉取项目详情

### 4.4 UserBadge

**职责**：从 `localStorage` 读写当前用户名，每次写请求随 `X-User-Name` header 发出。

**行为**：
- 初始渲染时读 `localStorage.user_name`，未设置时提示填写
- 点击后弹出 inline 输入框修改
- 修改后写回 `localStorage`，不请求后端

---

## 5. 状态管理

### 5.1 Zustand Store 结构

状态分为两块：**全局 UI 状态**（轻量）和**项目数据**（来自服务器，SSE 更新）。

```ts
// lib/state.ts

interface AppStore {
  // 当前用户名（镜像 localStorage）
  userName: string
  setUserName: (name: string) => void

  // 当前打开的项目（在 /projects/[id] 系列页面下有值）
  currentProject: Project | null
  setCurrentProject: (project: Project | null) => void
  updateProjectStatus: (status: ProjectStatus) => void

  // Shot 列表（由 SSE 增量更新）
  shots: Shot[]
  setShots: (shots: Shot[]) => void
  updateShot: (shotId: number, patch: Partial<Shot>) => void

  // 分镜审批页的多选状态
  selectedShotIds: Set<number>
  toggleShotSelection: (shotId: number) => void
  clearSelection: () => void

  // Toast 消息
  toasts: Toast[]
  addToast: (toast: Toast) => void
  removeToast: (id: string) => void
}
```

**原则**：服务器数据（project、shots）在首次加载时通过 `GET /api/projects/{id}` 写入 store，之后由 SSE 增量 patch，不做乐观更新。

### 5.2 SSE 驱动的状态更新

每类 SSE 事件对应 store 的一个 action：

| SSE 事件 | store 操作 |
|---|---|
| `state_snapshot` | `setCurrentProject` + `setShots`（初始化） |
| `state_change` | `updateProjectStatus` |
| `script_ready` | `updateProjectStatus(SCRIPT_REVIEW)` + `setShots` |
| `shot_started` | `updateShot(shotId, { status: 'prompt_generating' })` |
| `shot_progress` | `updateShot(shotId, { status: sub_status })` |
| `shot_completed` | `updateShot(shotId, { status: 'completed', video_path, last_frame_path })` |
| `shot_failed` | `updateShot(shotId, { status: 'failed', error_message })` |
| `all_shots_ready` | `updateProjectStatus(SHOT_REVIEW)` |
| `export_done` | `updateProjectStatus(EXPORTED)` + 更新 `final_video_path` |
| `pipeline_failed` | `updateProjectStatus(FAILED)` + `addToast(error)` |

### 5.3 页面级局部状态

以下状态不进 store，保留在各页面的 `useState`：

- 表单输入值（新建项目页）
- 编辑某 shot 的 modal 开关和临时值
- 正在发起请求的 loading flag（按钮禁用）

### 5.4 断层警告逻辑

分镜审批页，当用户勾选了需要重跑的 shot 集合 `S` 时，前端需计算可能引发视觉断层的下游镜头：

```ts
function computeCascadeWarnings(
  shots: Shot[],
  selectedIds: Set<number>
): Map<number, number[]> {
  // 返回 Map<被重跑的shotId, 受影响的下游shotId[]>
  const warnings = new Map<number, number[]>()

  for (const id of selectedIds) {
    const downstream: number[] = []
    let cursor = id + 1
    while (cursor <= shots.length) {
      const s = shots.find(s => s.shot_id === cursor)
      if (!s || !s.align_with_previous) break
      if (!selectedIds.has(cursor)) {
        downstream.push(cursor)
      }
      cursor++
    }
    if (downstream.length > 0) {
      warnings.set(id, downstream)
    }
  }
  return warnings
}
```

有警告时在底部操作区显示：
> "shot N 的下游 [N+1..M] 是连续镜头，只重跑 N 可能导致衔接断层。[一键追加]"

无下游对齐镜头时不显示任何提示。

---

## 6. API 客户端层

### 6.1 基础封装

`lib/api.ts` 封装所有后端请求，统一处理：
- Base URL（`process.env.NEXT_PUBLIC_API_BASE`）
- `X-User-Name` header（从 `localStorage` 读取）
- JSON 序列化 / 反序列化
- 错误格式统一解析（后端返回 `{"error": {"code", "message"}}`）

```ts
// 内部基础函数
async function request<T>(
  method: string,
  path: string,
  body?: unknown
): Promise<T>
```

### 6.2 API 方法列表

```ts
// 项目管理
api.listProjects(params?: { status?: string; creator?: string; sort?: string }): Promise<Project[]>
api.createProject(data: { title: string; theme_text: string }): Promise<{ project_id: string; status: ProjectStatus }>
api.getProject(id: string): Promise<ProjectDetail>
api.deleteProject(id: string): Promise<void>

// 参考图
api.uploadReferenceImages(id: string, files: File[], kind: 'character' | 'scene'): Promise<{ image_ids: string[] }>
api.deleteReferenceImage(id: string, imageId: string): Promise<void>

// Pipeline 控制
api.startPipeline(id: string): Promise<void>
api.regenerateScript(id: string): Promise<void>
api.patchStoryboard(id: string, data: Partial<Storyboard>): Promise<void>
api.approveScript(id: string): Promise<void>
api.regenerateShots(id: string, shotIds: number[]): Promise<void>
api.patchShot(id: string, shotId: number, data: { motion_prompt: string }): Promise<void>
api.exportVideo(id: string): Promise<void>
api.resetToScript(id: string): Promise<void>
api.resetProject(id: string): Promise<void>

// 资源下载（返回可直接用于 src 属性的 URL）
api.assetUrl(id: string, kind: string, file: string): string
api.finalVideoUrl(id: string): string
```

### 6.3 资源 URL

图片和视频不走 JS 请求，直接拼 URL 给 `<img src>` / `<video src>`：

```ts
// lib/api.ts
export function assetUrl(projectId: string, kind: string, file: string): string {
  return `${BASE}/api/projects/${projectId}/assets/${kind}/${file}`
}
```

---

## 7. SSE 实时推送

### 7.1 封装

`lib/sse.ts` 封装 `EventSource`，提供清晰的事件订阅接口：

```ts
interface SSEConnection {
  subscribe(event: SSEEventType, handler: (data: unknown) => void): () => void
  close(): void
}

export function createSSEConnection(projectId: string): SSEConnection
```

### 7.2 事件类型

```ts
type SSEEventType =
  | 'state_snapshot'
  | 'state_change'
  | 'script_ready'
  | 'shot_started'
  | 'shot_progress'
  | 'shot_completed'
  | 'shot_failed'
  | 'all_shots_ready'
  | 'export_done'
  | 'pipeline_failed'
```

### 7.3 使用方式

`<ProgressStream />` 组件统一管理 SSE 生命周期。各页面通过其 `onEvent` 回调监听感兴趣的事件：

```tsx
// /projects/[id]/script/page.tsx
<ProgressStream
  projectId={id}
  onEvent={(e) => {
    if (e.type === 'script_ready') {
      store.setShots(e.data.storyboard.shots)
      store.updateProjectStatus('script_review')
    }
  }}
/>
```

### 7.4 断线重连

`EventSource` 原生支持断线自动重连。重连后的处理：
1. 服务端在 SSE 连接建立时立即推送 `state_snapshot`（完整快照）
2. 客户端收到 `state_snapshot` 时覆盖更新 store，实现状态同步

---

## 8. TypeScript 类型定义

所有类型集中在 `lib/types.ts`，与后端 Pydantic schema 保持对齐。

```ts
// lib/types.ts

type ProjectStatus =
  | 'draft' | 'scripting' | 'script_review'
  | 'shot_generating' | 'shot_review'
  | 'exporting' | 'exported' | 'failed'

type ShotStatus =
  | 'pending' | 'prompt_generating' | 'video_generating'
  | 'completed' | 'failed'

interface Project {
  id: string
  title: string
  theme_text: string
  creator_name: string
  status: ProjectStatus
  scene_overview: string | null
  final_video_path: string | null
  error_message: string | null
  created_at: string
  updated_at: string
}

interface Shot {
  id: number
  project_id: string
  shot_id: number
  text: string
  shot_type: 'Close-up' | 'Medium Shot' | 'Wide Shot'
  visual_description: string
  shot_duration: 4 | 6 | 8
  status: ShotStatus
  align_with_previous: boolean
  motion_prompt: string | null
  first_frame_path: string | null
  video_path: string | null
  last_frame_path: string | null
  word_count_warning: boolean
  error_message: string | null
}

interface ReferenceImage {
  id: string
  project_id: string
  kind: 'character' | 'scene'
  filename: string
  storage_path: string
  order_index: number
}

interface ProjectDetail extends Project {
  shots: Shot[]
  reference_images: ReferenceImage[]
}

// SSE 事件
interface SSEEvent {
  type: SSEEventType
  data: unknown  // 用具体子类型收窄
}
```

---

## 9. UI 设计系统

### 9.1 调性

- **主色调**：冷色调蓝灰，信息密度偏高（生产力工具风格）
- **字体**：系统默认无衬线体
- **间距**：Tailwind 标准间距体系

### 9.2 shadcn/ui 组件使用

| 场景 | 组件 |
|---|---|
| 按钮 | `Button`（variant: default / outline / destructive / ghost） |
| 输入框 | `Input`, `Textarea` |
| 弹窗 / 模态 | `Dialog`（编辑 shot、图片 lightbox） |
| 状态徽章 | `Badge`（variant 映射 ProjectStatus / ShotStatus） |
| 下拉菜单 | `DropdownMenu`（卡片右上角操作菜单） |
| 工具提示 | `Tooltip`（智能断层提示） |
| 进度条 | `Progress` |
| 开关 | `Switch`（对齐开关 🔗 / ✂） |
| Toast | `Toaster` + `useToast`（错误提示） |

### 9.3 状态颜色规范

| 状态 | Badge 颜色 | 说明 |
|---|---|---|
| draft | gray | 未开始 |
| scripting / shot_generating / exporting | blue | 进行中 |
| script_review / shot_review | yellow | 等待用户操作 |
| exported | green | 完成 |
| failed | red | 错误 |
| shot.failed | red 边框 | ShotCard 失败样式 |
| word_count_warning | yellow 徽章 | 台词超字数提示 |

---

## 10. 错误处理

### 10.1 API 错误

`lib/api.ts` 统一抛出格式化错误，页面层 catch 后调用 `addToast`：

```ts
// 后端错误格式
interface APIError {
  code: string
  message: string
}

// 页面调用示例
try {
  await api.approveScript(id)
} catch (e) {
  addToast({ type: 'error', message: e.message })
}
```

### 10.2 SSE 断线

- `EventSource` 自动重连（浏览器内置）
- 重连成功后收到 `state_snapshot`，覆盖同步状态
- SHOT_GENERATING 状态下若 60 秒无任何 SSE 事件，显示警告 Toast 并手动拉取一次 `GET /api/projects/{id}`

### 10.3 FAILED 状态

- 项目入口页 `/projects/[id]` 展示 `error_message` 内容
- 提供 [重置项目] 按钮，调用 `POST /api/projects/{id}/reset`（回到 DRAFT）
- 重置后 router.replace 到 `/projects/{id}` 重新开始

---

## 11. 测试策略

### 11.1 组件单元测试（Vitest + RTL）

重点测试以下组件：

| 组件 | 测试重点 |
|---|---|
| `ShotCard` | variant 切换渲染、对齐开关状态、多选框 |
| `ProgressStream` | SSE 事件驱动状态更新、断线重连逻辑 |
| `UploadZone` | 文件数量限制、拖拽高亮、缩略图展示 |
| 断层警告逻辑 | `computeCascadeWarnings` 的边界条件（纯函数，直接测） |

### 11.2 TypeScript 构建检查

CI 必须通过 `tsc --noEmit`，无类型错误。

### 11.3 端到端测试（Playwright，MVP 后）

完整的 wizard 流程：创建 → 审批脚本 → 审批分镜 → 导出。
