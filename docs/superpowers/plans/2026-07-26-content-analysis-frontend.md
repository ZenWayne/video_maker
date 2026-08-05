# 内容分析前端（spec §10）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 为已完成的内容分析后端补上前端：分析列表 / 新建（上传视频）/ 详情（SSE 进度 + 逐样本 + brief 展示）三页，外加新建项目里的「挂载爆款简报」选择器。按 `design/shots.pen` 已画的设计稿实现。

**Architecture:** React 18 + TypeScript + react-router-dom v6 + Tailwind + zustand。复用现有 `src/lib/api.ts`（`request`/`uploadForm`，自动带 `X-User-Name`）、`components/ui/*` 原语、`HomePage`/`NewProjectPage`/`ProgressStream` 模式。新增 analysis 侧 SSE 包装（镜像 `lib/sse.ts`）。数据获取沿用现有命令式 `useState`+`useEffect`+轮询，不引入 react-query。

**Tech Stack:** React 18 · TS · react-router v6 · Tailwind 3 · zustand · base-ui/radix · lucide-react · Vitest · Playwright

**Design source:** `design/shots.pen` 底部「内容分析 · 界面设计」四屏 + 挂载器（Ⓐ列表 / Ⓑ新建 / Ⓒ详情 / Ⓓ简报）。
**Backend API:** `backend/app/api/content_analysis.py`（已实现，contract 固定）。

## Global Constraints

- **前端目录**：`frontend-vite/`。dev server 端口 4000，`/api` 代理到 8002（`vite.config.ts`）。
- **运行**：全栈已通过 `podman compose` 起在本机（backend 8002 / frontend 4000 / redis 6381 均在跑）。前端 `node_modules` 已装。
- **单测**：`npx vitest run <file>`（无 `test` script）。**e2e**：`npx playwright test <file>`，真后端、**绝不 mock 被测数据**（仅在触发计费模型处短路）。
- **API 只走** `request`/`uploadSingle`/`uploadForm`（自动带 `X-User-Name`）；**禁止**裸 `fetch` 绕过鉴权头。
- **状态徽章**用现有模式：`<Badge variant="secondary" className={statusColors[s]}>`，`statusColors` 是「`bg-*-100 text-*-700`」原始 Tailwind 类映射（镜像 `HomePage.tsx:22-42`）。
- **设计 token**：`--radius:0.5rem`（=8px）→ 用 `rounded-lg`；主色**不是** token（token 是近黑），蓝色主按钮/高亮用原始 `bg-blue-600`/`bg-blue-50`/`text-blue-700` 等，和 `NewProjectPage.tsx:124` 一致。
- **每个交互元素加 `data-testid`**（e2e 依赖），命名沿用现有风格（如 `create-analysis-submit`）。
- **snake_case 保留**：TS 接口字段与后端一致（`region_hint`/`brief_json`/`has_speech`…），沿用 `Project`/`Shot` 约定。
- **不改后端**。若发现需要后端改动，停下问，不擅自改。

---

## File Structure

**新建：**
- `frontend-vite/src/lib/analysisSse.ts` — analysis 侧 SSE 包装（镜像 `lib/sse.ts`）
- `frontend-vite/src/lib/analysisStatus.ts` — 状态 label/color 映射 + `parseBrief()`
- `frontend-vite/src/pages/AnalysesPage.tsx` — 列表（Ⓐ）
- `frontend-vite/src/pages/NewAnalysisPage.tsx` — 新建+上传（Ⓑ）
- `frontend-vite/src/pages/AnalysisDetailPage.tsx` — 详情：进度+逐样本+brief（Ⓒ+Ⓓ）
- `frontend-vite/src/components/BriefView.tsx` — 简报展示卡（Ⓓ，被详情页复用）
- `frontend-vite/src/components/AnalysisProgress.tsx` — 三步进度器 + 逐样本行（Ⓒ）
- 对应 `__tests__/*.test.tsx` 单测
- `frontend-vite/e2e/content-analysis.spec.ts` — Playwright e2e

**修改：**
- `frontend-vite/src/lib/types.ts` — 加 `ContentAnalysis`/`ReferenceSample`/状态联合类型；`Project`/`ProjectDetail` 加 `content_analysis_id`/`attached_brief_json`
- `frontend-vite/src/lib/api.ts` — 加 `createAnalysis`/`listAnalyses`/`getAnalysis`/`attachBrief`
- `frontend-vite/src/components/UploadZone.tsx` — 支持视频（新增 `accept`/`kind="video"`，不破坏图片用法）
- `frontend-vite/src/App.tsx` — 加 3 条路由
- `frontend-vite/src/pages/HomePage.tsx` — header 加「内容分析」入口
- `frontend-vite/src/pages/NewProjectPage.tsx` — 加「挂载爆款简报」选择器 + 提交后 `attachBrief`

---

## Task 1: 类型 + API 客户端

**Files:**
- Modify: `frontend-vite/src/lib/types.ts`（末尾追加；并给 `Project`/`ProjectDetail` 加两字段）
- Modify: `frontend-vite/src/lib/api.ts`（`api` 对象里加 4 个函数）
- Test: `frontend-vite/src/lib/__tests__/analysisApi.test.ts`

**Interfaces:**
- Produces: `ContentAnalysis`, `ReferenceSample`, `ContentAnalysisStatus`, `ReferenceSampleStatus`；`api.createAnalysis({title,regionHint?,files})`, `api.listAnalyses()`, `api.getAnalysis(id)`, `api.attachBrief(projectId, analysisId)`

- [ ] **Step 1: 加类型**

`frontend-vite/src/lib/types.ts` 末尾追加：
```ts
export type ContentAnalysisStatus = 'uploading' | 'transcribing' | 'analyzing' | 'completed' | 'failed'
export type ReferenceSampleStatus = 'pending' | 'transcribing' | 'transcribed' | 'failed'

export interface ReferenceSample {
  id: number
  analysis_id: string
  order_index: number
  video_path: string
  has_speech: boolean | null
  hook_text: string | null
  full_transcript: string | null
  language: string | null
  status: ReferenceSampleStatus
  error_message: string | null
  created_at: string
}

export interface ContentAnalysis {
  id: string
  title: string
  region_hint: string | null
  status: ContentAnalysisStatus
  brief_json: string | null
  error_message: string | null
  created_at: string
  updated_at: string
  samples: ReferenceSample[]
}

// 简报结构（brief_json 解析后）——镜像后端 CreationBrief
export interface CreationBrief {
  niche_summary: string
  sample_stats: { sample_n: number; no_speech_pct: number; sample_warning: string | null }
  hook_strategy: { common_hook_types: string[]; example_hooks: string[] }
  script_structure: { pacing: string; emotion: string; info_gap: string; cta: string }
  do: string[]
  dont: string[]
  screenwriter_directives: string
}
```
在现有 `Project` 与 `ProjectDetail` 接口里各加两字段（找到定义处，追加）：
```ts
  content_analysis_id?: string | null
  attached_brief_json?: string | null
```

- [ ] **Step 2: 加 API 函数**

`frontend-vite/src/lib/api.ts` 的 `api` 对象里追加（`uploadForm`/`request` 已存在于本文件）：
```ts
  createAnalysis: (data: { title: string; regionHint?: string; files: File[] }): Promise<ContentAnalysis> => {
    const form = new FormData()
    form.append('title', data.title)
    if (data.regionHint) form.append('region_hint', data.regionHint)
    data.files.forEach((f) => form.append('files', f))
    return uploadForm('/api/analyses', form)
  },
  listAnalyses: (): Promise<ContentAnalysis[]> =>
    request<{ analyses: ContentAnalysis[]; total: number }>('GET', '/api/analyses').then((d) => d.analyses),
  getAnalysis: (id: string): Promise<ContentAnalysis> =>
    request<ContentAnalysis>('GET', `/api/analyses/${id}`),
  attachBrief: (projectId: string, analysisId: string): Promise<ProjectDetail> =>
    request<ProjectDetail>('POST', `/api/projects/${projectId}/attach-brief`, { analysis_id: analysisId }),
```
在文件顶部的 type import 里补 `ContentAnalysis`（与现有 import 同一处）。

- [ ] **Step 3: 写测试（构造形状）**

`frontend-vite/src/lib/__tests__/analysisApi.test.ts`：mock `global.fetch`，断言 `createAnalysis` 发出 multipart（FormData 含 `title`/`region_hint`/多个 `files`）、`listAnalyses` 解包 `.analyses`、`attachBrief` POST body 为 `{analysis_id}`。镜像现有 `src/lib/__tests__/` 里对 `api` 的测法（先读一个现有 api 测试文件照抄 fetch mock 结构）。

- [ ] **Step 4: 跑测试**

Run: `cd frontend-vite && npx vitest run src/lib/__tests__/analysisApi.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend-vite/src/lib/types.ts frontend-vite/src/lib/api.ts frontend-vite/src/lib/__tests__/analysisApi.test.ts
git commit -m "feat(fe): content-analysis 类型 + API 客户端（createAnalysis/list/get/attachBrief）"
```

---

## Task 2: 状态映射 + 简报解析 + analysis SSE 包装

**Files:**
- Create: `frontend-vite/src/lib/analysisStatus.ts`
- Create: `frontend-vite/src/lib/analysisSse.ts`
- Test: `frontend-vite/src/lib/__tests__/analysisStatus.test.ts`

**Interfaces:**
- Produces: `ANALYSIS_STATUS_LABELS`, `ANALYSIS_STATUS_COLORS`, `SAMPLE_STATUS_LABELS`, `SAMPLE_STATUS_COLORS`, `parseBrief(json: string|null): CreationBrief | null`；`createAnalysisSSEConnection(analysisId): SSEConnection`

- [ ] **Step 1: 状态映射 + 解析**

`frontend-vite/src/lib/analysisStatus.ts`：
```ts
import type { ContentAnalysisStatus, ReferenceSampleStatus, CreationBrief } from './types'

export const ANALYSIS_STATUS_LABELS: Record<ContentAnalysisStatus, string> = {
  uploading: '上传中', transcribing: '转写中', analyzing: '归纳中', completed: '已完成', failed: '失败',
}
export const ANALYSIS_STATUS_COLORS: Record<ContentAnalysisStatus, string> = {
  uploading: 'bg-zinc-100 text-zinc-600',
  transcribing: 'bg-amber-100 text-amber-700',
  analyzing: 'bg-blue-100 text-blue-700',
  completed: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
}
export const SAMPLE_STATUS_LABELS: Record<ReferenceSampleStatus, string> = {
  pending: '待转写', transcribing: '转写中', transcribed: '已转写', failed: '失败',
}
export const SAMPLE_STATUS_COLORS: Record<ReferenceSampleStatus, string> = {
  pending: 'bg-zinc-100 text-zinc-500',
  transcribing: 'bg-blue-100 text-blue-700',
  transcribed: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
}

export function parseBrief(json: string | null | undefined): CreationBrief | null {
  if (!json) return null
  try { return JSON.parse(json) as CreationBrief } catch { return null }
}

// 三步进度器：转写 → 归纳 → 完成。返回每步的 done/active/pending。
export function analysisSteps(status: ContentAnalysisStatus) {
  const order: ContentAnalysisStatus[] = ['transcribing', 'analyzing', 'completed']
  const idx = status === 'failed' ? -1 : order.indexOf(status === 'uploading' ? 'transcribing' : status)
  return [
    { key: 'transcribing', label: '转写口播' },
    { key: 'analyzing', label: '联合归纳' },
    { key: 'completed', label: '完成' },
  ].map((s, i) => ({ ...s, state: idx < 0 ? 'pending' : i < idx ? 'done' : i === idx ? 'active' : 'pending' as const }))
}
```

- [ ] **Step 2: SSE 包装**

先读 `frontend-vite/src/lib/sse.ts` 全文，照抄其 `createSSEConnection` 结构，改 URL 与命名。`frontend-vite/src/lib/analysisSse.ts`：
```ts
// 镜像 lib/sse.ts，仅把 URL 指向 analysis stream。事件形状同为 { type, data }。
import { BASE } from './sse' // 若 BASE 未导出，则本文件内重定义 const BASE = import.meta.env.VITE_API_BASE || ''
export type { SSEConnection } from './sse'

export function createAnalysisSSEConnection(analysisId: string) {
  // …与 createSSEConnection 相同的 EventSource + subscribe/close 实现，
  // URL 改为 `${BASE}/api/analyses/${analysisId}/stream`
}
```
> 具体实现照抄 `sse.ts`（含 subscriber 集合、`onmessage` JSON 解析、`subscribe`/`close`）。若 `sse.ts` 的 `BASE`/类型未 export，则在本文件内复制那几行，不要 import 私有符号。

- [ ] **Step 3: 测试映射 + 解析 + 步骤**

`frontend-vite/src/lib/__tests__/analysisStatus.test.ts`：断言 `parseBrief` 对合法 JSON 返回对象、对 `null`/坏 JSON 返回 `null`；`analysisSteps('analyzing')` 中转写=done、归纳=active、完成=pending；`analysisSteps('failed')` 全 pending；颜色/label 映射键完整。

- [ ] **Step 4: 跑测试**

Run: `cd frontend-vite && npx vitest run src/lib/__tests__/analysisStatus.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend-vite/src/lib/analysisStatus.ts frontend-vite/src/lib/analysisSse.ts frontend-vite/src/lib/__tests__/analysisStatus.test.ts
git commit -m "feat(fe): 分析状态映射/简报解析/analysis SSE 包装"
```

---

## Task 3: UploadZone 支持视频

**Files:**
- Modify: `frontend-vite/src/components/UploadZone.tsx`
- Test: `frontend-vite/src/components/__tests__/UploadZone.video.test.tsx`

**Interfaces:**
- 现有：`<UploadZone kind="character"|"scene" maxFiles value onChange />`（图片，`accept image/*`，`file.type.startsWith('image/')` 过滤）
- Produces: 新增可选 `accept?: string`（默认 `image/*`）与允许 `kind="video"`；当视频模式时过滤 `file.type.startsWith('video/')`，label/hint/icon 走视频文案（「点击或拖拽上传视频」「MP4 / MOV」clapperboard 图标）。**图片用法保持不变**。

- [ ] **Step 1: 写测试（先失败）**

`__tests__/UploadZone.video.test.tsx`：渲染 `<UploadZone kind="video" .../>`，断言接受 `video/mp4` 文件、拒绝图片、显示视频文案；再渲染现有图片用法断言未回归（接受图片、拒绝视频）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/UploadZone.video.test.tsx`
Expected: FAIL

- [ ] **Step 3: 改 UploadZone**

读现有 `UploadZone.tsx` 全文。把写死的 `image/` 判定与文案改为按模式：新增 `accept` prop 与 `kind` 扩展到 `'video'`；用一个 `isVideo = kind === 'video'` 分支决定 `accept`、过滤前缀（`video/` vs `image/`）、icon（`clapperboard` vs `image-plus`）、主/副文案。保持既有 props 与默认行为不变（向后兼容）。

- [ ] **Step 4: 跑测试**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/UploadZone.video.test.tsx`
Expected: PASS。再跑既有 UploadZone 测试确认无回归：`npx vitest run src/components/__tests__/`（若存在 UploadZone 既有测试）。

- [ ] **Step 5: Commit**

```bash
git add frontend-vite/src/components/UploadZone.tsx frontend-vite/src/components/__tests__/UploadZone.video.test.tsx
git commit -m "feat(fe): UploadZone 支持视频模式（不破坏图片用法）"
```

---

## Task 4: 列表页 AnalysesPage（Ⓐ）+ 路由 + 入口

**Files:**
- Create: `frontend-vite/src/pages/AnalysesPage.tsx`
- Modify: `frontend-vite/src/App.tsx`（加路由）
- Modify: `frontend-vite/src/pages/HomePage.tsx`（header 加「内容分析」入口）
- Test: `frontend-vite/src/pages/__tests__/AnalysesPage.test.tsx`

**Interfaces:** Consumes `api.listAnalyses`, `ANALYSIS_STATUS_LABELS/COLORS`, `parseBrief`. 路由 `/analyses`。

- [ ] **Step 1: 写页面**

镜像 `HomePage.tsx` 结构。`AnalysesPage.tsx`：
- header（`data-testid="analyses-page"`）：返回 + 标题「内容分析」+ 右侧 `<Button onClick={()=>navigate('/analyses/new')} data-testid="new-analysis-btn">新建分析</Button>`
- 搜索框 + 状态筛选（可先只做搜索框占位，筛选用 select）
- `useEffect` 拉 `api.listAnalyses()`，5s 轮询（照抄 HomePage 的 interval）
- 卡片网格 `grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4`：每卡（`Card`/`CardHeader`/`CardContent`）显示 title、`<Badge variant="secondary" className={ANALYSIS_STATUS_COLORS[a.status]}>{ANALYSIS_STATUS_LABELS[a.status]}</Badge>`、`{a.samples.length} 个样本 · 日期`、简报摘要（`parseBrief(a.brief_json)?.niche_summary` 或状态文案）、点击卡片 `navigate('/analyses/'+a.id)`。每卡加 `data-testid={'analysis-card-'+a.id}`。
- 设计参照 shots.pen Ⓐ（截图见 design/shots.pen）。

- [ ] **Step 2: 加路由 + 入口**

`App.tsx` `<Routes>` 内加：
```tsx
<Route path="/analyses" element={<AnalysesPage />} />
<Route path="/analyses/new" element={<NewAnalysisPage />} />
<Route path="/analyses/:id" element={<AnalysisDetailPage />} />
```
（`NewAnalysisPage`/`AnalysisDetailPage` 在 Task 5/6 建；本 task 可先只加 `/analyses` 路由，或加全部三条并临时 import——若临时 import 未建文件会编译失败，故本 task 只加 `/analyses` 一条，Task 5/6 各自补自己的路由行。）
`HomePage.tsx` header 加一个入口按钮/链接：`<Button variant="ghost" onClick={()=>navigate('/analyses')} data-testid="nav-analyses">内容分析</Button>`。

- [ ] **Step 3: 写测试**

`__tests__/AnalysesPage.test.tsx`：mock `api.listAnalyses` 返回 2 条（completed + analyzing），渲染（包 `MemoryRouter`），断言两卡标题、状态徽章文案、样本数出现；completed 卡显示 niche_summary。镜像现有 `pages/__tests__/` 的渲染+mock 写法。

- [ ] **Step 4: 跑测试**

Run: `cd frontend-vite && npx vitest run src/pages/__tests__/AnalysesPage.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend-vite/src/pages/AnalysesPage.tsx frontend-vite/src/App.tsx frontend-vite/src/pages/HomePage.tsx frontend-vite/src/pages/__tests__/AnalysesPage.test.tsx
git commit -m "feat(fe): 内容分析列表页 + 路由 + 首页入口"
```

---

## Task 5: 新建页 NewAnalysisPage（Ⓑ）

**Files:**
- Create: `frontend-vite/src/pages/NewAnalysisPage.tsx`
- Modify: `frontend-vite/src/App.tsx`（加 `/analyses/new` 路由）
- Test: `frontend-vite/src/pages/__tests__/NewAnalysisPage.test.tsx`

**Interfaces:** Consumes `api.createAnalysis`, `<UploadZone kind="video">`. 成功后 `navigate('/analyses/'+created.id)`。

- [ ] **Step 1: 写页面**

镜像 `NewProjectPage.tsx`。`NewAnalysisPage.tsx`：
- header：返回 + 「新建内容分析」
- form-card：
  - 分析名称（`Input`，`data-testid="analysis-title-input"`，必填 + 红星）
  - 目标语言（可选）（`Input`，`data-testid="analysis-lang-input"`）+ hint「非模型支持的语言码（如 en-US、美国）会被拒绝」
  - `<UploadZone kind="video" maxFiles={20} value={videos} onChange={setVideos} data-testid="analysis-upload" />`，label「爆款视频样本（建议 ≥3 条）」
  - 蓝色合规说明块「只分析口播转写文本…音频转写后即删」
  - actions：取消（`variant="outline"`）+ 「开始分析」（`data-testid="create-analysis-submit"`）
- 提交 handler：`isSubmitting` guard → `const a = await api.createAnalysis({title, regionHint, files: videos})` → `navigate('/analyses/'+a.id)`；try/catch 用现有 toast 显示后端 400（如非法语言码）。
- 设计参照 shots.pen Ⓑ。

- [ ] **Step 2: 加路由**

`App.tsx` 加 `<Route path="/analyses/new" element={<NewAnalysisPage />} />` + import。

- [ ] **Step 3: 写测试**

`__tests__/NewAnalysisPage.test.tsx`：mock `api.createAnalysis` resolve `{id:'a1',...}`；填标题、加一个视频文件、点提交，断言 `createAnalysis` 收到正确 args 且导航到 `/analyses/a1`。再断言后端抛 400 时显示错误 toast（mock reject）。

- [ ] **Step 4: 跑测试**

Run: `cd frontend-vite && npx vitest run src/pages/__tests__/NewAnalysisPage.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend-vite/src/pages/NewAnalysisPage.tsx frontend-vite/src/App.tsx frontend-vite/src/pages/__tests__/NewAnalysisPage.test.tsx
git commit -m "feat(fe): 内容分析新建页（上传视频 + 语言校验提示）"
```

---

## Task 6: 详情页 AnalysisDetailPage（Ⓒ）+ 进度组件 + 简报组件（Ⓓ）

**Files:**
- Create: `frontend-vite/src/components/AnalysisProgress.tsx`（三步进度器 + 逐样本行）
- Create: `frontend-vite/src/components/BriefView.tsx`（简报展示）
- Create: `frontend-vite/src/pages/AnalysisDetailPage.tsx`
- Modify: `frontend-vite/src/App.tsx`（加 `/analyses/:id` 路由）
- Test: `frontend-vite/src/components/__tests__/BriefView.test.tsx`、`AnalysisProgress.test.tsx`

**Interfaces:** `AnalysisProgress({ analysis })`；`BriefView({ brief, onAttach?, onExport? })`；页面 Consumes `api.getAnalysis` + `createAnalysisSSEConnection`。

- [ ] **Step 1: AnalysisProgress 组件**

`AnalysisProgress.tsx`：入参 `analysis: ContentAnalysis`。渲染：
- 三步进度器：`analysisSteps(analysis.status)` → 每步圆点（done=green+check、active=blue+序号、pending=zinc 边框+序号）+ label + 之间连接线。
- 覆盖度行：`已转写 {n有人声}/{总} · 无人声占比 {pct}%`（从 samples 计算）。
- 逐样本列表：每 `sample` 一行（缩略图占位 + `viral_xx.mp4`/时长/语言 + hook_text 预览 + `<Badge className={SAMPLE_STATUS_COLORS[sample.status]}>`）。`has_speech===false` 的行灰显 + 「无人声·跳过」。
- 设计参照 shots.pen Ⓒ。每行 `data-testid={'sample-row-'+sample.id}`。

- [ ] **Step 2: BriefView 组件**

`BriefView.tsx`：入参 `brief: CreationBrief`, 可选 `onAttach`/`onExport`。渲染 shots.pen Ⓓ：niche_summary、样本统计 chips（`sample_n`/`no_speech_pct`/警告）、钩子策略（type chips + 引用示例）、脚本结构 2×2（pacing/emotion/info_gap/cta）、做到/避免两列、蓝色高亮的 `screenwriter_directives` 块（带「→ screenwriter」标签）、底部「导出 Markdown」+「挂载到项目」。`data-testid="brief-view"`。挂载按钮 `data-testid="brief-attach-btn"`。

- [ ] **Step 3: 详情页**

`AnalysisDetailPage.tsx`（镜像 `ProgressStream.tsx` 的 SSE 生命周期）：
- `useParams` 取 id；初次 `api.getAnalysis(id)` 填 state。
- `createAnalysisSSEConnection(id)`：`subscribe('state_snapshot', d => setAnalysis(d))`；对任意进度事件（如 `analysis_progress`）**重新 `api.getAnalysis(id)`** 刷新（稳健，不假设事件粒度）。加 30s 停滞兜底轮询（status 属 `transcribing`/`analyzing` 时），照抄 ProgressStream 兜底。cleanup 里 `sse.close()`。
- 布局：header（返回 + title + 状态徽章）；status ∈ 进行中/失败 → 渲染 `<AnalysisProgress analysis={a}/>`；status==='completed' → 渲染 `<BriefView brief={parseBrief(a.brief_json)!} onAttach=... />`（挂载先跳到项目选择或提示，MVP 可 toast「请在新建项目时挂载」）。failed → 显示 `error_message`。
- 加 `/analyses/:id` 路由 + import。

- [ ] **Step 4: 测试**

`BriefView.test.tsx`：传一份完整 `CreationBrief`，断言 niche_summary、钩子类型、四项结构、do/dont、directives、挂载按钮都渲染。`AnalysisProgress.test.tsx`：传一个 analyzing 状态 + 4 样本（含 has_speech=false 一条），断言进度器 active=归纳、覆盖度文案、逐样本行与灰显跳过行。

- [ ] **Step 5: 跑测试**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/BriefView.test.tsx src/components/__tests__/AnalysisProgress.test.tsx`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add frontend-vite/src/components/AnalysisProgress.tsx frontend-vite/src/components/BriefView.tsx frontend-vite/src/pages/AnalysisDetailPage.tsx frontend-vite/src/App.tsx frontend-vite/src/components/__tests__/BriefView.test.tsx frontend-vite/src/components/__tests__/AnalysisProgress.test.tsx
git commit -m "feat(fe): 内容分析详情页（SSE 进度 + 逐样本 + 简报展示）"
```

---

## Task 7: 新建项目「挂载爆款简报」选择器

**Files:**
- Modify: `frontend-vite/src/pages/NewProjectPage.tsx`
- Test: `frontend-vite/src/pages/__tests__/NewProjectPage.attachBrief.test.tsx`

**Interfaces:** Consumes `api.listAnalyses`（筛 `status==='completed'`）, `api.attachBrief`.

- [ ] **Step 1: 加选择器 + 提交接线**

`NewProjectPage.tsx`：
- 挂载 `useEffect` 拉 `api.listAnalyses()`，筛 `completed` 存 state。
- 表单加可选字段「挂载爆款简报（可选）」：一个 select（`data-testid="attach-brief-select"`），选项为「无」+ 各已完成分析（显示 title + 绿点 + 已完成）。设计参照 shots.pen 挂载器。
- 提交流程：现有「create → upload → start → navigate」链**在 create 之后、navigate 之前**插入：若选了分析，`await api.attachBrief(created.id, selectedAnalysisId)`（失败不阻断主流程，toast 提示）。
- helper 文案「选中后简报以快照写入项目，日后简报改动不回溯污染已建项目」。

- [ ] **Step 2: 测试**

`__tests__/NewProjectPage.attachBrief.test.tsx`：mock `listAnalyses` 返回 1 条 completed；渲染，选中它，走提交，断言 `api.attachBrief(newProjectId, analysisId)` 被调用。再断言不选时不调用 attachBrief。

- [ ] **Step 3: 跑测试**

Run: `cd frontend-vite && npx vitest run src/pages/__tests__/NewProjectPage.attachBrief.test.tsx`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add frontend-vite/src/pages/NewProjectPage.tsx frontend-vite/src/pages/__tests__/NewProjectPage.attachBrief.test.tsx
git commit -m "feat(fe): 新建项目挂载爆款简报选择器 + attachBrief 接线"
```

---

## Task 8: Playwright e2e（真后端，不 mock 数据）

**Files:**
- Create: `frontend-vite/e2e/content-analysis.spec.ts`

**Interfaces:** 真后端 `/api/*`；镜像 `e2e/projects.spec.ts` 的 `request` 播种 + `finally` 清理。

- [ ] **Step 1: 写 e2e**

`e2e/content-analysis.spec.ts`（镜像 `projects.spec.ts`）。**避免触发计费 Gemini + 慢 ASR** 的策略：不通过 UI 真跑分析管线，而是**直接向真实 DB 播种一个 completed 分析**（含 `brief_json`），再驱动 UI 断言展示——与本仓库 trim e2e「真实播种、零 mock」一致。具体：
- 用 `podman exec` 进 backend 容器，向真实 DB 插一条 `content_analyses`（status=completed, brief_json=一段真实 JSON）+ 几条 `reference_samples`（含 has_speech true/false）。（参照 `CLAUDE.md` e2e 播种约定 `podman exec` 进容器插行。）
- 测 1：访问 `/analyses`，断言列表出现该分析卡 + 已完成徽章。
- 测 2：点进 `/analyses/{id}`，断言 `brief-view` 渲染 niche_summary / 钩子 / directives。
- 测 3（不播种、走真 UI）：`/analyses/new` 填 `region_hint=en-US` 提交，断言前端把后端 400 呈现为可见错误（真实 `POST /api/analyses` 返回 400）——这条**不触发管线**（400 在入队前）。
- `finally` 删除播种数据（`podman exec` 删行 或 若有 delete 端点则用之——当前无 delete 端点，故用 `podman exec` DELETE）。

> 若播种/清理经 `podman exec` 在此环境不可行，退化为仅保留「测 3」（真 UI + 真 400，无播种、无计费），并在报告说明其余用例因环境限制未跑。

- [ ] **Step 2: 跑 e2e**

Run: `cd frontend-vite && npx playwright test e2e/content-analysis.spec.ts`
Expected: PASS（全栈已在跑）

- [ ] **Step 3: Commit**

```bash
git add frontend-vite/e2e/content-analysis.spec.ts
git commit -m "test(e2e): 内容分析列表/详情/简报展示 + 语言校验（真后端播种，零数据 mock）"
```

---

## Self-Review（作者自查）

**Spec §10 覆盖：** 分析列表→T4；建分析+上传→T3(视频)+T5；SSE 进度+逐样本→T6(AnalysisProgress+详情页)；brief 展示→T6(BriefView)；挂载 brief 选择器→T7。全覆盖。
**API 契约一致：** `createAnalysis` multipart 字段 `title`/`region_hint`/`files` 对齐后端 `Form/File`；`listAnalyses` 解包 `.analyses`；`attachBrief` body `{analysis_id}`；SSE `state_snapshot` + 事件后 re-fetch（不假设 per-sample 事件粒度，规避探查存疑点）。
**约束：** 全走 `request`/`uploadForm`（带鉴权头）；徽章用 `variant=secondary`+原始色类；testid 齐全；e2e 真后端且规避计费（播种 completed 分析 / 400 在入队前）。
**类型一致：** `ContentAnalysisStatus`/`ReferenceSampleStatus` 与后端枚举逐值对齐；`CreationBrief` 与后端 `CreationBrief` 字段对齐。
**依赖顺序：** T1(类型/api)→T2(状态/sse)→T3(上传)→T4/5/6(页面)→T7(挂载)→T8(e2e)。路由分散在 T4/5/6 各自添加，避免临时 import 未建文件导致编译失败。
