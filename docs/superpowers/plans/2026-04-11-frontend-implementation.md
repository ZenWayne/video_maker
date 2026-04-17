# Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Next.js 14 frontend for the Video Maker tool — project list, wizard flow (new → script review → shot review → export), real-time SSE progress, and shared state.

**Architecture:** Bottom-up: types → api → sse → state → components → pages. Lib layer is the source of truth for all types and server contracts. Components are pure UI, pages orchestrate lib + components.

**Tech Stack:** Next.js 14 App Router, TypeScript strict, Tailwind CSS, shadcn/ui, Zustand, native fetch + EventSource, Vitest + RTL

---

## File Map

| File | Responsibility |
|---|---|
| `frontend/lib/types.ts` | All shared TypeScript types |
| `frontend/lib/api.ts` | REST API client |
| `frontend/lib/sse.ts` | EventSource wrapper |
| `frontend/lib/state.ts` | Zustand store + `computeCascadeWarnings` |
| `frontend/components/UserBadge.tsx` | Username read/write from localStorage |
| `frontend/components/UploadZone.tsx` | Drag-and-drop image picker |
| `frontend/components/ProgressStream.tsx` | SSE subscriber + progress UI |
| `frontend/components/ShotCard.tsx` | Shot display, 3 variants |
| `frontend/app/layout.tsx` | Root layout with Toaster |
| `frontend/app/page.tsx` | Project list with 5s polling |
| `frontend/app/projects/new/page.tsx` | Create project form |
| `frontend/app/projects/[id]/page.tsx` | Status-based router |
| `frontend/app/projects/[id]/script/page.tsx` | Script review wizard step |
| `frontend/app/projects/[id]/shots/page.tsx` | Shot review wizard step |
| `frontend/app/projects/[id]/export/page.tsx` | Export wizard step |
| `frontend/Dockerfile` | Multi-stage production build |
| `frontend/.env.local` | API base URL |

---

## Task 1: Scaffold with create-next-app

**Files:** Creates `frontend/` directory and all scaffolding

- [ ] **Step 1: Run create-next-app**

Run in `/home/wayne/tools/video_maker/`:
```bash
npx create-next-app@latest frontend \
  --typescript --tailwind --eslint --app \
  --src-dir=no --import-alias="@/*" --no-git
```
When prompted for defaults, accept all defaults (App Router: Yes).

- [ ] **Step 2: Install Zustand**

```bash
cd frontend && npm install zustand lucide-react
```

- [ ] **Step 3: Init shadcn/ui**

```bash
npx shadcn@latest init --defaults
```

- [ ] **Step 4: Add shadcn components**

```bash
npx shadcn@latest add button input textarea dialog badge \
  dropdown-menu tooltip progress switch sonner checkbox select
```

- [ ] **Step 5: Create .env.local**

```bash
echo "NEXT_PUBLIC_API_BASE=http://localhost:8000" > .env.local
```

- [ ] **Step 6: Verify build**

```bash
npm run build
```
Expected: Build succeeds with no errors.

- [ ] **Step 7: Commit**

```bash
cd ..
git add frontend/
git commit -m "feat: scaffold Next.js 14 frontend with shadcn/ui and Zustand"
```

---

## Task 2: lib/types.ts

**Files:**
- Create: `frontend/lib/types.ts`

- [ ] **Step 1: Write types.ts**

```ts
// frontend/lib/types.ts

export type ProjectStatus =
  | 'draft'
  | 'scripting'
  | 'script_review'
  | 'shot_generating'
  | 'shot_review'
  | 'exporting'
  | 'exported'
  | 'failed'

export type ShotStatus =
  | 'pending'
  | 'prompt_generating'
  | 'video_generating'
  | 'completed'
  | 'failed'

export interface Project {
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

export interface Shot {
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

export interface ReferenceImage {
  id: string
  project_id: string
  kind: 'character' | 'scene'
  filename: string
  storage_path: string
  order_index: number
}

export interface ProjectDetail extends Project {
  shots: Shot[]
  reference_images: ReferenceImage[]
}

export type SSEEventType =
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

export interface SSEEvent {
  type: SSEEventType
  data: unknown
}

export interface APIError {
  code: string
  message: string
}

export interface Toast {
  id: string
  type: 'success' | 'error' | 'info'
  message: string
}

export interface Storyboard {
  scene_overview: string
  shots: Shot[]
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend && npx tsc --noEmit
```
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/lib/types.ts
git commit -m "feat: add shared TypeScript types"
```

---

## Task 3: lib/api.ts

**Files:**
- Create: `frontend/lib/api.ts`

- [ ] **Step 1: Write api.ts**

```ts
// frontend/lib/api.ts
import type { Project, ProjectDetail, ProjectStatus, Shot, ReferenceImage, Storyboard } from './types'

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000'

function getUserName(): string {
  if (typeof window === 'undefined') return ''
  return localStorage.getItem('user_name') ?? ''
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {
    'X-User-Name': getUserName(),
  }

  let bodyInit: BodyInit | undefined
  if (body instanceof FormData) {
    bodyInit = body
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    bodyInit = JSON.stringify(body)
  }

  const res = await fetch(`${BASE}${path}`, { method, headers, body: bodyInit })

  if (!res.ok) {
    const json = await res.json().catch(() => ({}))
    const errMsg = json?.error?.message ?? `HTTP ${res.status}`
    throw new Error(errMsg)
  }

  if (res.status === 204 || res.headers.get('content-length') === '0') {
    return undefined as T
  }
  return res.json() as Promise<T>
}

function qs(params: Record<string, string | undefined>): string {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== '') p.set(k, v)
  }
  const s = p.toString()
  return s ? '?' + s : ''
}

export const api = {
  // Projects
  listProjects: (params?: { status?: string; creator?: string; sort?: string }) =>
    request<Project[]>('GET', `/api/projects${qs({ status: params?.status, creator: params?.creator, sort: params?.sort })}`),

  createProject: (data: { title: string; theme_text: string }) =>
    request<{ project_id: string; status: ProjectStatus }>('POST', '/api/projects', data),

  getProject: (id: string) =>
    request<ProjectDetail>('GET', `/api/projects/${id}`),

  deleteProject: (id: string) =>
    request<void>('DELETE', `/api/projects/${id}`),

  // Reference images
  uploadReferenceImages: (id: string, files: File[], kind: 'character' | 'scene') => {
    const fd = new FormData()
    fd.append('kind', kind)
    for (const f of files) fd.append('files', f)
    return request<{ image_ids: string[] }>('POST', `/api/projects/${id}/reference-images`, fd)
  },

  deleteReferenceImage: (id: string, imageId: string) =>
    request<void>('DELETE', `/api/projects/${id}/reference-images/${imageId}`),

  // Pipeline control
  startPipeline: (id: string) =>
    request<void>('POST', `/api/projects/${id}/start`),

  regenerateScript: (id: string) =>
    request<void>('POST', `/api/projects/${id}/regenerate-script`),

  patchStoryboard: (id: string, data: Partial<Pick<Storyboard, 'scene_overview'>>) =>
    request<void>('PATCH', `/api/projects/${id}/storyboard`, data),

  approveScript: (id: string) =>
    request<void>('POST', `/api/projects/${id}/approve-script`),

  regenerateShots: (id: string, shotIds: number[]) =>
    request<void>('POST', `/api/projects/${id}/regenerate-shots`, { shot_ids: shotIds }),

  patchShot: (id: string, shotId: number, data: { motion_prompt: string }) =>
    request<void>('PATCH', `/api/projects/${id}/shots/${shotId}`, data),

  exportVideo: (id: string) =>
    request<void>('POST', `/api/projects/${id}/export`),

  resetToScript: (id: string) =>
    request<void>('POST', `/api/projects/${id}/reset-to-script`),

  resetProject: (id: string) =>
    request<void>('POST', `/api/projects/${id}/reset`),

  // Asset URLs (for use in <img src> / <video src>)
  assetUrl: (projectId: string, kind: string, file: string): string =>
    `${BASE}/api/projects/${projectId}/assets/${kind}/${file}`,

  finalVideoUrl: (id: string): string =>
    `${BASE}/api/projects/${id}/final-video`,
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend && npx tsc --noEmit
```
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/lib/api.ts
git commit -m "feat: add REST API client"
```

---

## Task 4: lib/sse.ts

**Files:**
- Create: `frontend/lib/sse.ts`

- [ ] **Step 1: Write sse.ts**

```ts
// frontend/lib/sse.ts
import type { SSEEventType } from './types'

type Handler = (data: unknown) => void

export interface SSEConnection {
  subscribe(event: SSEEventType, handler: Handler): () => void
  close(): void
}

const SSE_EVENTS: SSEEventType[] = [
  'state_snapshot',
  'state_change',
  'script_ready',
  'shot_started',
  'shot_progress',
  'shot_completed',
  'shot_failed',
  'all_shots_ready',
  'export_done',
  'pipeline_failed',
]

export function createSSEConnection(projectId: string): SSEConnection {
  const BASE = process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000'
  const es = new EventSource(`${BASE}/api/projects/${projectId}/events`)
  const listeners = new Map<SSEEventType, Set<Handler>>()

  for (const eventType of SSE_EVENTS) {
    es.addEventListener(eventType, (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data)
        const handlers = listeners.get(eventType)
        if (handlers) {
          for (const h of handlers) h(data)
        }
      } catch {
        // Ignore malformed messages
      }
    })
  }

  return {
    subscribe(event, handler) {
      if (!listeners.has(event)) listeners.set(event, new Set())
      listeners.get(event)!.add(handler)
      return () => {
        listeners.get(event)?.delete(handler)
      }
    },
    close() {
      es.close()
    },
  }
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend && npx tsc --noEmit
```
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/lib/sse.ts
git commit -m "feat: add SSE EventSource wrapper"
```

---

## Task 5: lib/state.ts

**Files:**
- Create: `frontend/lib/state.ts`

- [ ] **Step 1: Write state.ts**

```ts
// frontend/lib/state.ts
import { create } from 'zustand'
import type { Project, Shot, Toast, ProjectStatus } from './types'

interface AppStore {
  userName: string
  setUserName: (name: string) => void

  currentProject: Project | null
  setCurrentProject: (project: Project | null) => void
  updateProjectStatus: (status: ProjectStatus) => void

  shots: Shot[]
  setShots: (shots: Shot[]) => void
  updateShot: (shotId: number, patch: Partial<Shot>) => void

  selectedShotIds: Set<number>
  toggleShotSelection: (shotId: number) => void
  clearSelection: () => void

  toasts: Toast[]
  addToast: (toast: Omit<Toast, 'id'>) => void
  removeToast: (id: string) => void
}

export const useAppStore = create<AppStore>((set) => ({
  userName: typeof window !== 'undefined' ? (localStorage.getItem('user_name') ?? '') : '',
  setUserName: (name) => set({ userName: name }),

  currentProject: null,
  setCurrentProject: (project) => set({ currentProject: project }),
  updateProjectStatus: (status) =>
    set((s) =>
      s.currentProject ? { currentProject: { ...s.currentProject, status } } : {}
    ),

  shots: [],
  setShots: (shots) => set({ shots }),
  updateShot: (shotId, patch) =>
    set((s) => ({
      shots: s.shots.map((shot) =>
        shot.shot_id === shotId ? { ...shot, ...patch } : shot
      ),
    })),

  selectedShotIds: new Set<number>(),
  toggleShotSelection: (shotId) =>
    set((s) => {
      const next = new Set(s.selectedShotIds)
      if (next.has(shotId)) next.delete(shotId)
      else next.add(shotId)
      return { selectedShotIds: next }
    }),
  clearSelection: () => set({ selectedShotIds: new Set<number>() }),

  toasts: [],
  addToast: (toast) =>
    set((s) => ({
      toasts: [...s.toasts, { ...toast, id: crypto.randomUUID() }],
    })),
  removeToast: (id) =>
    set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}))

export function computeCascadeWarnings(
  shots: Shot[],
  selectedIds: Set<number>
): Map<number, number[]> {
  const warnings = new Map<number, number[]>()

  for (const id of selectedIds) {
    const downstream: number[] = []
    let cursor = id + 1
    while (cursor <= shots.length) {
      const s = shots.find((shot) => shot.shot_id === cursor)
      if (!s || !s.align_with_previous) break
      if (!selectedIds.has(cursor)) downstream.push(cursor)
      cursor++
    }
    if (downstream.length > 0) warnings.set(id, downstream)
  }
  return warnings
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend && npx tsc --noEmit
```
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/lib/state.ts
git commit -m "feat: add Zustand store and computeCascadeWarnings"
```

---

## Task 6: Unit test for computeCascadeWarnings

**Files:**
- Create: `frontend/lib/__tests__/computeCascadeWarnings.test.ts`

- [ ] **Step 1: Install Vitest**

```bash
cd frontend && npm install -D vitest @vitejs/plugin-react jsdom @testing-library/react @testing-library/jest-dom
```

Add to `frontend/package.json` under `"scripts"`:
```json
"test": "vitest run"
```

Add `frontend/vitest.config.ts`:
```ts
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
  },
  resolve: {
    alias: { '@': path.resolve(__dirname, '.') },
  },
})
```

- [ ] **Step 2: Write failing tests**

```bash
mkdir -p frontend/lib/__tests__
```

```ts
// frontend/lib/__tests__/computeCascadeWarnings.test.ts
import { describe, it, expect } from 'vitest'
import { computeCascadeWarnings } from '../state'
import type { Shot } from '../types'

function makeShot(shot_id: number, align: boolean): Shot {
  return {
    id: shot_id,
    project_id: 'p1',
    shot_id,
    text: '',
    shot_type: 'Medium Shot',
    visual_description: '',
    shot_duration: 4,
    status: 'completed',
    align_with_previous: align,
    motion_prompt: null,
    first_frame_path: null,
    video_path: null,
    last_frame_path: null,
    word_count_warning: false,
    error_message: null,
  }
}

describe('computeCascadeWarnings', () => {
  it('returns empty map when no shots selected', () => {
    const shots = [makeShot(1, false), makeShot(2, true)]
    const result = computeCascadeWarnings(shots, new Set())
    expect(result.size).toBe(0)
  })

  it('returns warning when downstream shots are aligned and not selected', () => {
    const shots = [makeShot(1, false), makeShot(2, true), makeShot(3, true)]
    const result = computeCascadeWarnings(shots, new Set([1]))
    expect(result.get(1)).toEqual([2, 3])
  })

  it('excludes downstream shots that are also selected', () => {
    const shots = [makeShot(1, false), makeShot(2, true), makeShot(3, true)]
    const result = computeCascadeWarnings(shots, new Set([1, 2]))
    // shot 2 is selected so not a warning; shot 3 is aligned and unselected → warning for shot 1
    expect(result.get(1)).toEqual([3])
  })

  it('stops at non-aligned shot', () => {
    const shots = [makeShot(1, false), makeShot(2, true), makeShot(3, false), makeShot(4, true)]
    const result = computeCascadeWarnings(shots, new Set([1]))
    // shot 3 is not aligned so chain breaks
    expect(result.get(1)).toEqual([2])
  })

  it('returns no warning when downstream shot is not aligned', () => {
    const shots = [makeShot(1, false), makeShot(2, false)]
    const result = computeCascadeWarnings(shots, new Set([1]))
    expect(result.size).toBe(0)
  })
})
```

- [ ] **Step 3: Run tests (verify they pass)**

```bash
cd frontend && npm test
```
Expected: 5 passing tests.

- [ ] **Step 4: Commit**

```bash
git add frontend/lib/__tests__/ frontend/vitest.config.ts frontend/package.json
git commit -m "test: add unit tests for computeCascadeWarnings"
```

---

## Task 7: components/UserBadge.tsx

**Files:**
- Create: `frontend/components/UserBadge.tsx`

- [ ] **Step 1: Write UserBadge.tsx**

```tsx
// frontend/components/UserBadge.tsx
'use client'
import { useState } from 'react'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { useAppStore } from '@/lib/state'

export function UserBadge() {
  const { userName, setUserName } = useAppStore()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')

  function startEdit() {
    setDraft(userName)
    setEditing(true)
  }

  function save() {
    const name = draft.trim()
    if (name) {
      localStorage.setItem('user_name', name)
      setUserName(name)
    }
    setEditing(false)
  }

  if (editing) {
    return (
      <Input
        autoFocus
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={save}
        onKeyDown={(e) => { if (e.key === 'Enter') save() }}
        className="h-8 w-36"
        placeholder="输入用户名"
      />
    )
  }

  return (
    <Button variant="ghost" size="sm" onClick={startEdit} className="h-8">
      {userName || <span className="text-muted-foreground italic">设置用户名</span>}
    </Button>
  )
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend && npx tsc --noEmit
```
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/components/UserBadge.tsx
git commit -m "feat: add UserBadge component"
```

---

## Task 8: components/UploadZone.tsx

**Files:**
- Create: `frontend/components/UploadZone.tsx`

- [ ] **Step 1: Write UploadZone.tsx**

```tsx
// frontend/components/UploadZone.tsx
'use client'
import { useRef, useState } from 'react'
import { X } from 'lucide-react'

interface UploadZoneProps {
  kind: 'character' | 'scene'
  maxFiles: number
  value: File[]
  onChange: (files: File[]) => void
}

export function UploadZone({ kind, maxFiles, value, onChange }: UploadZoneProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)

  function addFiles(incoming: FileList | null) {
    if (!incoming) return
    const images = Array.from(incoming).filter((f) => f.type.startsWith('image/'))
    const merged = [...value, ...images].slice(0, maxFiles)
    onChange(merged)
  }

  function removeFile(index: number) {
    onChange(value.filter((_, i) => i !== index))
  }

  return (
    <div className="space-y-3">
      <div
        onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); addFiles(e.dataTransfer.files) }}
        onClick={() => inputRef.current?.click()}
        className={`border-2 border-dashed rounded-lg p-8 text-center cursor-pointer transition-colors select-none ${
          dragging ? 'border-blue-500 bg-blue-50/50' : 'border-border hover:border-muted-foreground/50'
        }`}
      >
        <input
          ref={inputRef}
          type="file"
          accept="image/*"
          multiple
          className="hidden"
          onChange={(e) => addFiles(e.target.files)}
        />
        <p className="text-sm text-muted-foreground">
          {kind === 'character' ? '角色参考图' : '场景参考图'}
        </p>
        <p className="text-xs text-muted-foreground mt-1">
          拖拽或点击上传（{value.length}/{maxFiles}）
        </p>
      </div>

      {value.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {value.map((file, i) => (
            <div key={i} className="relative group w-20 h-20">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={URL.createObjectURL(file)}
                alt={file.name}
                className="w-full h-full object-cover rounded border"
              />
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); removeFile(i) }}
                className="absolute -top-1.5 -right-1.5 bg-destructive text-destructive-foreground rounded-full p-0.5 opacity-0 group-hover:opacity-100 transition-opacity"
              >
                <X size={10} />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend && npx tsc --noEmit
```
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/components/UploadZone.tsx
git commit -m "feat: add UploadZone drag-and-drop component"
```

---

## Task 9: components/ProgressStream.tsx

**Files:**
- Create: `frontend/components/ProgressStream.tsx`

- [ ] **Step 1: Write ProgressStream.tsx**

```tsx
// frontend/components/ProgressStream.tsx
'use client'
import { useEffect, useRef } from 'react'
import { createSSEConnection } from '@/lib/sse'
import { useAppStore } from '@/lib/state'
import { api } from '@/lib/api'
import type { SSEEvent, SSEEventType } from '@/lib/types'
import { Progress } from '@/components/ui/progress'

interface ProgressStreamProps {
  projectId: string
  onEvent?: (event: SSEEvent) => void
}

const ALL_EVENTS: SSEEventType[] = [
  'state_snapshot', 'state_change', 'script_ready',
  'shot_started', 'shot_progress', 'shot_completed',
  'shot_failed', 'all_shots_ready', 'export_done', 'pipeline_failed',
]

export function ProgressStream({ projectId, onEvent }: ProgressStreamProps) {
  const store = useAppStore()
  const lastEventAt = useRef(Date.now())

  useEffect(() => {
    const conn = createSSEConnection(projectId)

    function dispatch(type: SSEEventType, data: unknown) {
      lastEventAt.current = Date.now()
      onEvent?.({ type, data })

      switch (type) {
        case 'state_snapshot': {
          const d = data as { project: any; storyboard?: { shots: any[] } }
          store.setCurrentProject(d.project)
          if (d.storyboard?.shots) store.setShots(d.storyboard.shots)
          break
        }
        case 'state_change':
          store.updateProjectStatus((data as any).status)
          break
        case 'script_ready': {
          const d = data as { storyboard: { shots: any[] } }
          store.updateProjectStatus('script_review')
          store.setShots(d.storyboard.shots)
          break
        }
        case 'shot_started':
          store.updateShot((data as any).shot_id, { status: 'prompt_generating' })
          break
        case 'shot_progress':
          store.updateShot((data as any).shot_id, { status: (data as any).sub_status })
          break
        case 'shot_completed': {
          const d = data as any
          store.updateShot(d.shot_id, {
            status: 'completed',
            video_path: d.video_path,
            last_frame_path: d.last_frame_path,
          })
          break
        }
        case 'shot_failed':
          store.updateShot((data as any).shot_id, {
            status: 'failed',
            error_message: (data as any).error_message,
          })
          break
        case 'all_shots_ready':
          store.updateProjectStatus('shot_review')
          break
        case 'export_done':
          store.updateProjectStatus('exported')
          break
        case 'pipeline_failed':
          store.updateProjectStatus('failed')
          store.addToast({ type: 'error', message: (data as any).error_message ?? 'Pipeline failed' })
          break
      }
    }

    const unsubs = ALL_EVENTS.map((type) =>
      conn.subscribe(type, (data) => dispatch(type, data))
    )

    // 60s silence → warn + manual fetch
    const timer = setInterval(async () => {
      if (Date.now() - lastEventAt.current > 60_000) {
        store.addToast({ type: 'info', message: '检查服务器状态...' })
        try {
          const project = await api.getProject(projectId)
          store.setCurrentProject(project)
          store.setShots(project.shots)
        } catch {
          // ignore
        }
        lastEventAt.current = Date.now()
      }
    }, 10_000)

    return () => {
      unsubs.forEach((u) => u())
      conn.close()
      clearInterval(timer)
    }
  }, [projectId]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="space-y-2 py-4">
      <p className="text-sm text-muted-foreground animate-pulse">处理中，请稍候...</p>
      <Progress className="h-1.5" />
    </div>
  )
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend && npx tsc --noEmit
```
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/components/ProgressStream.tsx
git commit -m "feat: add ProgressStream SSE subscriber component"
```

---

## Task 10: components/ShotCard.tsx

**Files:**
- Create: `frontend/components/ShotCard.tsx`

- [ ] **Step 1: Write ShotCard.tsx**

```tsx
// frontend/components/ShotCard.tsx
'use client'
import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import type { Shot } from '@/lib/types'

interface ShotCardProps {
  shot: Shot
  variant: 'script' | 'review' | 'generating'
  selected?: boolean
  onSelect?: (shotId: number) => void
  onEditScript?: (shotId: number) => void
  onEditPrompt?: (shotId: number, prompt: string) => void
  onViewFirstFrame?: (shotId: number) => void
}

function StatusBadge({ status }: { status: Shot['status'] }) {
  const variants: Record<Shot['status'], 'default' | 'secondary' | 'destructive' | 'outline'> = {
    pending: 'secondary',
    prompt_generating: 'default',
    video_generating: 'default',
    completed: 'outline',
    failed: 'destructive',
  }
  return <Badge variant={variants[status]}>{status}</Badge>
}

export function ShotCard({
  shot,
  variant,
  selected,
  onSelect,
  onEditScript,
  onEditPrompt,
  onViewFirstFrame,
}: ShotCardProps) {
  const [promptDraft, setPromptDraft] = useState(shot.motion_prompt ?? '')
  const [editingPrompt, setEditingPrompt] = useState(false)

  if (variant === 'generating') {
    return (
      <div className={`border rounded-lg p-4 space-y-1 ${shot.status === 'failed' ? 'border-destructive' : ''}`}>
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium text-muted-foreground">镜 {shot.shot_id}</span>
          <StatusBadge status={shot.status} />
        </div>
        {shot.status === 'failed' && shot.error_message && (
          <p className="text-xs text-destructive">{shot.error_message}</p>
        )}
      </div>
    )
  }

  if (variant === 'script') {
    return (
      <div className="border rounded-lg p-4 space-y-2">
        <div className="flex items-start justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold">镜 {shot.shot_id}</span>
            <Badge variant="outline" className="text-xs">{shot.shot_type}</Badge>
            <Badge variant="outline" className="text-xs">{shot.shot_duration}s</Badge>
            {shot.word_count_warning && (
              <Badge className="text-xs bg-yellow-100 text-yellow-800 border-yellow-300">字数超限</Badge>
            )}
          </div>
          <div className="flex items-center gap-3 shrink-0">
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Switch
                checked={shot.align_with_previous}
                disabled
                className="scale-75"
              />
              <span>{shot.align_with_previous ? '🔗' : '✂'}</span>
            </div>
            <Button size="sm" variant="outline" onClick={() => onEditScript?.(shot.shot_id)}>
              编辑
            </Button>
          </div>
        </div>
        <p className="text-sm leading-relaxed">{shot.text}</p>
      </div>
    )
  }

  // review variant
  return (
    <div className={`border rounded-lg p-4 space-y-3 ${shot.status === 'failed' ? 'border-destructive' : ''}`}>
      <div className="flex items-start gap-3">
        {onSelect && (
          <Checkbox
            checked={selected ?? false}
            onCheckedChange={() => onSelect(shot.shot_id)}
            className="mt-0.5"
          />
        )}
        <div className="flex-1 min-w-0 space-y-3">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold">镜 {shot.shot_id}</span>
            {shot.align_with_previous && (
              <Badge variant="outline" className="text-xs">🔗 对齐</Badge>
            )}
            {shot.status === 'failed' && <StatusBadge status="failed" />}
          </div>

          {shot.video_path && (
            <video
              src={shot.video_path}
              controls
              className="w-full rounded border bg-black max-h-52"
            />
          )}

          <div className="space-y-1">
            <p className="text-xs text-muted-foreground">运镜提示词</p>
            {editingPrompt ? (
              <div className="space-y-1.5">
                <Textarea
                  value={promptDraft}
                  onChange={(e) => setPromptDraft(e.target.value)}
                  rows={3}
                  className="text-sm"
                />
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    onClick={() => {
                      onEditPrompt?.(shot.shot_id, promptDraft)
                      setEditingPrompt(false)
                    }}
                  >
                    保存
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => setEditingPrompt(false)}>
                    取消
                  </Button>
                </div>
              </div>
            ) : (
              <p
                className="text-sm cursor-pointer hover:bg-muted rounded px-1 py-0.5 min-h-[2rem]"
                onClick={() => setEditingPrompt(true)}
              >
                {shot.motion_prompt || <span className="text-muted-foreground italic">点击编辑</span>}
              </p>
            )}
          </div>

          <div className="flex gap-2">
            {onViewFirstFrame && shot.first_frame_path && (
              <Button size="sm" variant="outline" onClick={() => onViewFirstFrame(shot.shot_id)}>
                查看首帧
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend && npx tsc --noEmit
```
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/components/ShotCard.tsx
git commit -m "feat: add ShotCard component with script/review/generating variants"
```

---

## Task 11: app/layout.tsx

**Files:**
- Modify: `frontend/app/layout.tsx`

- [ ] **Step 1: Replace layout.tsx**

```tsx
// frontend/app/layout.tsx
import type { Metadata } from 'next'
import { Inter } from 'next/font/google'
import './globals.css'
import { Toaster } from '@/components/ui/sonner'

const inter = Inter({ subsets: ['latin'] })

export const metadata: Metadata = {
  title: 'Video Maker',
  description: '一句话到成片的视频制作工具',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh">
      <body className={`${inter.className} bg-background text-foreground`}>
        {children}
        <Toaster position="top-right" />
      </body>
    </html>
  )
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend && npx tsc --noEmit
```
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/app/layout.tsx
git commit -m "feat: configure root layout with Toaster"
```

---

## Task 12: app/page.tsx — Project List

**Files:**
- Modify: `frontend/app/page.tsx`

- [ ] **Step 1: Write app/page.tsx**

```tsx
// frontend/app/page.tsx
'use client'
import { useEffect, useState, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import Link from 'next/link'
import { api } from '@/lib/api'
import type { Project, ProjectStatus } from '@/lib/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { UserBadge } from '@/components/UserBadge'
import {
  DropdownMenu, DropdownMenuContent,
  DropdownMenuItem, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { MoreVertical, Plus } from 'lucide-react'

const STATUS_LABEL: Record<ProjectStatus, string> = {
  draft: '草稿',
  scripting: '生成脚本中',
  script_review: '待审批脚本',
  shot_generating: '生成视频中',
  shot_review: '待审批分镜',
  exporting: '导出中',
  exported: '已完成',
  failed: '失败',
}

const STATUS_VARIANT: Record<ProjectStatus, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  draft: 'secondary',
  scripting: 'default',
  script_review: 'outline',
  shot_generating: 'default',
  shot_review: 'outline',
  exporting: 'default',
  exported: 'outline',
  failed: 'destructive',
}

const ALL_STATUSES = Object.keys(STATUS_LABEL) as ProjectStatus[]

export default function HomePage() {
  const router = useRouter()
  const [projects, setProjects] = useState<Project[]>([])
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState<string>('')
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const data = await api.listProjects({ status: statusFilter || undefined })
      setProjects(data)
    } catch {
      // silently ignore polling errors
    } finally {
      setLoading(false)
    }
  }, [statusFilter])

  useEffect(() => {
    load()
    const id = setInterval(load, 5000)
    return () => clearInterval(id)
  }, [load])

  async function handleDelete(id: string) {
    if (!confirm('确认删除该项目？')) return
    try {
      await api.deleteProject(id)
      await load()
    } catch (e: any) {
      alert(e.message)
    }
  }

  const filtered = projects.filter((p) => {
    if (!search) return true
    return p.title.includes(search) || p.creator_name.includes(search)
  })

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="sticky top-0 z-10 bg-white border-b px-6 py-3 flex items-center justify-between">
        <h1 className="text-lg font-semibold tracking-tight">Video Maker</h1>
        <div className="flex items-center gap-3">
          <UserBadge />
          <Button asChild size="sm">
            <Link href="/projects/new">
              <Plus size={14} className="mr-1" />
              新建项目
            </Link>
          </Button>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-6 py-8 space-y-6">
        <div className="flex items-center gap-3 flex-wrap">
          <Input
            placeholder="搜索标题 / 创建者"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="max-w-xs h-9"
          />
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="h-9 border border-input rounded-md px-3 text-sm bg-background"
          >
            <option value="">全部状态</option>
            {ALL_STATUSES.map((s) => (
              <option key={s} value={s}>{STATUS_LABEL[s]}</option>
            ))}
          </select>
        </div>

        {loading ? (
          <p className="text-sm text-muted-foreground">加载中...</p>
        ) : filtered.length === 0 ? (
          <div className="text-center py-20 text-muted-foreground">
            <p>暂无项目</p>
            <Button asChild variant="outline" className="mt-4">
              <Link href="/projects/new">创建第一个项目</Link>
            </Button>
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {filtered.map((project) => (
              <div
                key={project.id}
                className="bg-white border rounded-xl p-5 space-y-3 hover:shadow-sm transition-shadow cursor-pointer"
                onClick={() => router.push(`/projects/${project.id}`)}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <h3 className="font-medium text-sm truncate">{project.title}</h3>
                    <p className="text-xs text-muted-foreground mt-0.5">{project.creator_name}</p>
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    <Badge variant={STATUS_VARIANT[project.status]} className="text-xs">
                      {STATUS_LABEL[project.status]}
                    </Badge>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild onClick={(e) => e.stopPropagation()}>
                        <Button variant="ghost" size="icon" className="h-7 w-7">
                          <MoreVertical size={13} />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem onClick={(e) => { e.stopPropagation(); router.push(`/projects/${project.id}`) }}>
                          打开
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          className="text-destructive focus:text-destructive"
                          onClick={(e) => { e.stopPropagation(); handleDelete(project.id) }}
                        >
                          删除
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                </div>
                <p className="text-xs text-muted-foreground line-clamp-2 leading-relaxed">
                  {project.theme_text}
                </p>
                <p className="text-xs text-muted-foreground">
                  {new Date(project.created_at).toLocaleDateString('zh-CN', {
                    year: 'numeric', month: 'short', day: 'numeric',
                  })}
                </p>
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  )
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend && npx tsc --noEmit
```
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/app/page.tsx
git commit -m "feat: add project list homepage with 5s polling"
```

---

## Task 13: app/projects/new/page.tsx

**Files:**
- Create: `frontend/app/projects/new/page.tsx`

- [ ] **Step 1: Create directory and write page**

```bash
mkdir -p frontend/app/projects/new
```

```tsx
// frontend/app/projects/new/page.tsx
'use client'
import { useState } from 'react'
import { useRouter } from 'next/navigation'
import { api } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { UploadZone } from '@/components/UploadZone'
import { toast } from 'sonner'
import { ArrowLeft } from 'lucide-react'
import Link from 'next/link'

export default function NewProjectPage() {
  const router = useRouter()
  const [title, setTitle] = useState('')
  const [theme, setTheme] = useState('')
  const [characterFiles, setCharacterFiles] = useState<File[]>([])
  const [sceneFiles, setSceneFiles] = useState<File[]>([])
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!title.trim()) return toast.error('请输入项目标题')
    if (!theme.trim()) return toast.error('请输入视频主题')
    if (characterFiles.length === 0) return toast.error('请上传至少一张角色参考图')

    setSubmitting(true)
    try {
      // Step 1: Create project
      const { project_id } = await api.createProject({ title: title.trim(), theme_text: theme.trim() })

      // Step 2: Upload reference images
      await api.uploadReferenceImages(project_id, characterFiles, 'character')
      if (sceneFiles.length > 0) {
        await api.uploadReferenceImages(project_id, sceneFiles, 'scene')
      }

      // Step 3: Start pipeline
      await api.startPipeline(project_id)

      router.push(`/projects/${project_id}/script`)
    } catch (e: any) {
      toast.error(e.message ?? '创建失败')
      setSubmitting(false)
    }
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b px-6 py-3 flex items-center gap-3">
        <Button asChild variant="ghost" size="icon" className="h-8 w-8">
          <Link href="/"><ArrowLeft size={16} /></Link>
        </Button>
        <h1 className="text-lg font-semibold">新建项目</h1>
      </header>

      <main className="max-w-2xl mx-auto px-6 py-10">
        <form onSubmit={handleSubmit} className="space-y-8">
          <div className="space-y-2">
            <label className="text-sm font-medium">项目标题</label>
            <Input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="例：产品发布会宣传片"
              disabled={submitting}
            />
          </div>

          <div className="space-y-2">
            <label className="text-sm font-medium">视频主题</label>
            <Textarea
              value={theme}
              onChange={(e) => setTheme(e.target.value)}
              placeholder="用一两句话描述视频内容，例：展示新款手机的轻薄设计和强劲性能"
              rows={3}
              disabled={submitting}
            />
          </div>

          <div className="space-y-2">
            <label className="text-sm font-medium">
              角色参考图 <span className="text-destructive">*</span>
            </label>
            <UploadZone
              kind="character"
              maxFiles={3}
              value={characterFiles}
              onChange={setCharacterFiles}
            />
          </div>

          <div className="space-y-2">
            <label className="text-sm font-medium">
              场景参考图 <span className="text-muted-foreground text-xs">（可选）</span>
            </label>
            <UploadZone
              kind="scene"
              maxFiles={3}
              value={sceneFiles}
              onChange={setSceneFiles}
            />
          </div>

          <Button type="submit" className="w-full" disabled={submitting}>
            {submitting ? '创建中...' : '创建并开始生成脚本'}
          </Button>
        </form>
      </main>
    </div>
  )
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend && npx tsc --noEmit
```
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/app/projects/new/
git commit -m "feat: add new project creation page"
```

---

## Task 14: app/projects/[id]/page.tsx — Status Router

**Files:**
- Create: `frontend/app/projects/[id]/page.tsx`

- [ ] **Step 1: Create directory and write page**

```bash
mkdir -p "frontend/app/projects/[id]"
```

```tsx
// frontend/app/projects/[id]/page.tsx
'use client'
import { useEffect, useState } from 'react'
import { useRouter, useParams } from 'next/navigation'
import { api } from '@/lib/api'
import type { ProjectDetail } from '@/lib/types'
import { Button } from '@/components/ui/button'
import { toast } from 'sonner'

export default function ProjectRouterPage() {
  const { id } = useParams<{ id: string }>()
  const router = useRouter()
  const [project, setProject] = useState<ProjectDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [resetting, setResetting] = useState(false)

  useEffect(() => {
    api.getProject(id).then((p) => {
      setProject(p)
      setLoading(false)

      switch (p.status) {
        case 'scripting':
        case 'script_review':
          router.replace(`/projects/${id}/script`)
          break
        case 'shot_generating':
        case 'shot_review':
          router.replace(`/projects/${id}/shots`)
          break
        case 'exporting':
        case 'exported':
          router.replace(`/projects/${id}/export`)
          break
        // 'draft' and 'failed' stay on this page
      }
    }).catch(() => {
      toast.error('无法加载项目')
      setLoading(false)
    })
  }, [id, router])

  async function handleStart() {
    if (!project) return
    try {
      await api.startPipeline(id)
      router.push(`/projects/${id}/script`)
    } catch (e: any) {
      toast.error(e.message)
    }
  }

  async function handleReset() {
    setResetting(true)
    try {
      await api.resetProject(id)
      router.replace(`/projects/${id}`)
    } catch (e: any) {
      toast.error(e.message)
      setResetting(false)
    }
  }

  if (loading) {
    return <div className="flex items-center justify-center min-h-screen text-muted-foreground">加载中...</div>
  }

  if (!project) {
    return <div className="flex items-center justify-center min-h-screen text-destructive">项目不存在</div>
  }

  return (
    <div className="max-w-2xl mx-auto px-6 py-20 text-center space-y-6">
      <h2 className="text-2xl font-semibold">{project.title}</h2>
      <p className="text-muted-foreground">{project.theme_text}</p>

      {project.status === 'draft' && (
        <Button size="lg" onClick={handleStart}>
          开始生成脚本
        </Button>
      )}

      {project.status === 'failed' && (
        <div className="space-y-4">
          <div className="rounded-lg border border-destructive bg-destructive/5 p-4 text-left">
            <p className="text-sm font-medium text-destructive">处理失败</p>
            <p className="text-sm text-muted-foreground mt-1">{project.error_message}</p>
          </div>
          <Button variant="destructive" onClick={handleReset} disabled={resetting}>
            {resetting ? '重置中...' : '重置项目'}
          </Button>
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend && npx tsc --noEmit
```
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add "frontend/app/projects/[id]/page.tsx"
git commit -m "feat: add project status router page"
```

---

## Task 15: app/projects/[id]/script/page.tsx

**Files:**
- Create: `frontend/app/projects/[id]/script/page.tsx`

- [ ] **Step 1: Create directory and write page**

```bash
mkdir -p "frontend/app/projects/[id]/script"
```

```tsx
// frontend/app/projects/[id]/script/page.tsx
'use client'
import { useEffect, useState } from 'react'
import { useRouter, useParams } from 'next/navigation'
import { api } from '@/lib/api'
import { useAppStore } from '@/lib/state'
import { ProgressStream } from '@/components/ProgressStream'
import { ShotCard } from '@/components/ShotCard'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import type { SSEEvent } from '@/lib/types'
import { toast } from 'sonner'
import { ArrowLeft } from 'lucide-react'
import Link from 'next/link'

export default function ScriptPage() {
  const { id } = useParams<{ id: string }>()
  const router = useRouter()
  const { currentProject, shots, setCurrentProject, setShots } = useAppStore()
  const [overview, setOverview] = useState('')
  const [savingOverview, setSavingOverview] = useState(false)
  const [approving, setApproving] = useState(false)
  const [regenerating, setRegenerating] = useState(false)

  useEffect(() => {
    api.getProject(id).then((p) => {
      setCurrentProject(p)
      setShots(p.shots)
      setOverview(p.scene_overview ?? '')
    }).catch(() => toast.error('无法加载项目'))
  }, [id]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (currentProject?.scene_overview != null) {
      setOverview(currentProject.scene_overview)
    }
  }, [currentProject?.scene_overview])

  function handleSSEEvent(e: SSEEvent) {
    if (e.type === 'script_ready') {
      // state updated in ProgressStream; refresh overview from store
    }
  }

  async function handleSaveOverview() {
    setSavingOverview(true)
    try {
      await api.patchStoryboard(id, { scene_overview: overview })
    } catch (e: any) {
      toast.error(e.message)
    } finally {
      setSavingOverview(false)
    }
  }

  async function handleRegenerate() {
    setRegenerating(true)
    try {
      await api.regenerateScript(id)
    } catch (e: any) {
      toast.error(e.message)
      setRegenerating(false)
    }
  }

  async function handleApprove() {
    setApproving(true)
    try {
      await api.approveScript(id)
      router.push(`/projects/${id}/shots`)
    } catch (e: any) {
      toast.error(e.message)
      setApproving(false)
    }
  }

  const status = currentProject?.status

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b px-6 py-3 flex items-center gap-3">
        <Button asChild variant="ghost" size="icon" className="h-8 w-8">
          <Link href="/"><ArrowLeft size={16} /></Link>
        </Button>
        <div>
          <h1 className="text-base font-semibold">{currentProject?.title ?? '...'}</h1>
          <p className="text-xs text-muted-foreground">脚本审批</p>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-6 py-8 space-y-6">
        {(status === 'scripting' || !status) && (
          <div className="bg-white border rounded-xl p-6">
            <p className="text-sm font-medium mb-4">正在生成脚本...</p>
            <ProgressStream projectId={id} onEvent={handleSSEEvent} />
          </div>
        )}

        {status === 'script_review' && (
          <>
            <div className="bg-white border rounded-xl p-6 space-y-3">
              <label className="text-sm font-medium">场景概述</label>
              <Textarea
                value={overview}
                onChange={(e) => setOverview(e.target.value)}
                rows={3}
                onBlur={handleSaveOverview}
                disabled={savingOverview}
              />
            </div>

            <div className="space-y-3">
              {shots.map((shot) => (
                <ShotCard key={shot.shot_id} shot={shot} variant="script" />
              ))}
            </div>

            <div className="flex items-center gap-3 pt-2">
              <Button
                variant="outline"
                onClick={handleRegenerate}
                disabled={regenerating || approving}
              >
                {regenerating ? '重新生成中...' : '重新生成脚本'}
              </Button>
              <Button
                onClick={handleApprove}
                disabled={approving || regenerating}
                className="ml-auto"
              >
                {approving ? '提交中...' : '通过，开始生成视频 →'}
              </Button>
            </div>
          </>
        )}
      </main>
    </div>
  )
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend && npx tsc --noEmit
```
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add "frontend/app/projects/[id]/script/"
git commit -m "feat: add script review page"
```

---

## Task 16: app/projects/[id]/shots/page.tsx

**Files:**
- Create: `frontend/app/projects/[id]/shots/page.tsx`

- [ ] **Step 1: Create directory and write page**

```bash
mkdir -p "frontend/app/projects/[id]/shots"
```

```tsx
// frontend/app/projects/[id]/shots/page.tsx
'use client'
import { useEffect, useState } from 'react'
import { useRouter, useParams } from 'next/navigation'
import { api } from '@/lib/api'
import { useAppStore, computeCascadeWarnings } from '@/lib/state'
import { ProgressStream } from '@/components/ProgressStream'
import { ShotCard } from '@/components/ShotCard'
import { Button } from '@/components/ui/button'
import { toast } from 'sonner'
import { ArrowLeft, AlertTriangle } from 'lucide-react'
import Link from 'next/link'

export default function ShotsPage() {
  const { id } = useParams<{ id: string }>()
  const router = useRouter()
  const {
    currentProject, shots, selectedShotIds,
    setCurrentProject, setShots, toggleShotSelection, clearSelection,
  } = useAppStore()
  const [regenerating, setRegenerating] = useState(false)
  const [exporting, setExporting] = useState(false)

  useEffect(() => {
    api.getProject(id).then((p) => {
      setCurrentProject(p)
      setShots(p.shots)
    }).catch(() => toast.error('无法加载项目'))
    return () => clearSelection()
  }, [id]) // eslint-disable-line react-hooks/exhaustive-deps

  async function handleEditPrompt(shotId: number, prompt: string) {
    try {
      await api.patchShot(id, shotId, { motion_prompt: prompt })
    } catch (e: any) {
      toast.error(e.message)
    }
  }

  async function handleRegenerate() {
    if (selectedShotIds.size === 0) return toast.error('请选择需要重跑的镜头')
    setRegenerating(true)
    try {
      await api.regenerateShots(id, Array.from(selectedShotIds))
      clearSelection()
    } catch (e: any) {
      toast.error(e.message)
      setRegenerating(false)
    }
  }

  async function handleExport() {
    setExporting(true)
    try {
      await api.exportVideo(id)
      router.push(`/projects/${id}/export`)
    } catch (e: any) {
      toast.error(e.message)
      setExporting(false)
    }
  }

  async function handleResetToScript() {
    try {
      await api.resetToScript(id)
      router.push(`/projects/${id}/script`)
    } catch (e: any) {
      toast.error(e.message)
    }
  }

  const status = currentProject?.status
  const warnings = computeCascadeWarnings(shots, selectedShotIds)
  const hasFailedShots = shots.some((s) => s.status === 'failed')
  const warningEntries = Array.from(warnings.entries())

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b px-6 py-3 flex items-center gap-3">
        <Button asChild variant="ghost" size="icon" className="h-8 w-8">
          <Link href="/"><ArrowLeft size={16} /></Link>
        </Button>
        <div>
          <h1 className="text-base font-semibold">{currentProject?.title ?? '...'}</h1>
          <p className="text-xs text-muted-foreground">分镜视频审批</p>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-6 py-8 space-y-6">
        {status === 'shot_generating' && (
          <div className="bg-white border rounded-xl p-6 space-y-4">
            <p className="text-sm font-medium">正在生成分镜视频...</p>
            <ProgressStream projectId={id} />
            <div className="space-y-2 mt-4">
              {shots.map((shot) => (
                <ShotCard key={shot.shot_id} shot={shot} variant="generating" />
              ))}
            </div>
          </div>
        )}

        {status === 'shot_review' && (
          <>
            <div className="space-y-3">
              {shots.map((shot) => (
                <ShotCard
                  key={shot.shot_id}
                  shot={shot}
                  variant="review"
                  selected={selectedShotIds.has(shot.shot_id)}
                  onSelect={toggleShotSelection}
                  onEditPrompt={handleEditPrompt}
                />
              ))}
            </div>

            {warningEntries.length > 0 && (
              <div className="rounded-lg border border-yellow-300 bg-yellow-50 p-4 space-y-2">
                <div className="flex items-center gap-2 text-yellow-800">
                  <AlertTriangle size={16} />
                  <span className="text-sm font-medium">断层警告</span>
                </div>
                {warningEntries.map(([shotId, downstream]) => (
                  <div key={shotId} className="text-sm text-yellow-800 flex items-center justify-between">
                    <span>
                      镜 {shotId} 的下游 [{downstream.join(', ')}] 是连续镜头，只重跑可能导致衔接断层
                    </span>
                    <Button
                      size="sm"
                      variant="outline"
                      className="ml-3 shrink-0 border-yellow-400 text-yellow-800 hover:bg-yellow-100"
                      onClick={() => downstream.forEach(toggleShotSelection)}
                    >
                      一键追加
                    </Button>
                  </div>
                ))}
              </div>
            )}

            <div className="flex items-center gap-3 pt-2 flex-wrap">
              <Button variant="outline" onClick={handleResetToScript}>
                退回修改脚本
              </Button>
              <Button
                variant="outline"
                onClick={handleRegenerate}
                disabled={selectedShotIds.size === 0 || regenerating}
              >
                {regenerating ? '重跑中...' : `重跑选中的镜 (${selectedShotIds.size})`}
              </Button>
              <Button
                onClick={handleExport}
                disabled={exporting || hasFailedShots}
                className="ml-auto"
                title={hasFailedShots ? '存在失败镜头，请先重跑' : undefined}
              >
                {exporting ? '导出中...' : '全部通过，导出 →'}
              </Button>
            </div>
          </>
        )}
      </main>
    </div>
  )
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend && npx tsc --noEmit
```
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add "frontend/app/projects/[id]/shots/"
git commit -m "feat: add shot review page with cascade warnings"
```

---

## Task 17: app/projects/[id]/export/page.tsx

**Files:**
- Create: `frontend/app/projects/[id]/export/page.tsx`

- [ ] **Step 1: Create directory and write page**

```bash
mkdir -p "frontend/app/projects/[id]/export"
```

```tsx
// frontend/app/projects/[id]/export/page.tsx
'use client'
import { useEffect } from 'react'
import { useRouter, useParams } from 'next/navigation'
import { api } from '@/lib/api'
import { useAppStore } from '@/lib/state'
import { ProgressStream } from '@/components/ProgressStream'
import { Button } from '@/components/ui/button'
import { toast } from 'sonner'
import { ArrowLeft, Download } from 'lucide-react'
import Link from 'next/link'

export default function ExportPage() {
  const { id } = useParams<{ id: string }>()
  const router = useRouter()
  const { currentProject, setCurrentProject, setShots } = useAppStore()

  useEffect(() => {
    api.getProject(id).then((p) => {
      setCurrentProject(p)
      setShots(p.shots)
    }).catch(() => toast.error('无法加载项目'))
  }, [id]) // eslint-disable-line react-hooks/exhaustive-deps

  const status = currentProject?.status

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b px-6 py-3 flex items-center gap-3">
        <Button asChild variant="ghost" size="icon" className="h-8 w-8">
          <Link href="/"><ArrowLeft size={16} /></Link>
        </Button>
        <div>
          <h1 className="text-base font-semibold">{currentProject?.title ?? '...'}</h1>
          <p className="text-xs text-muted-foreground">成片导出</p>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-6 py-8 space-y-6">
        {status === 'exporting' && (
          <div className="bg-white border rounded-xl p-6">
            <p className="text-sm font-medium mb-4">正在合并导出成片...</p>
            <ProgressStream
              projectId={id}
              onEvent={(e) => {
                if (e.type === 'export_done') router.refresh()
              }}
            />
          </div>
        )}

        {status === 'exported' && currentProject && (
          <div className="space-y-6">
            <div className="bg-white border rounded-xl p-6 space-y-4">
              <video
                src={api.finalVideoUrl(id)}
                controls
                className="w-full rounded-lg bg-black max-h-96"
              />
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="font-medium">{currentProject.title}</h3>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    创建者：{currentProject.creator_name} ·{' '}
                    {new Date(currentProject.created_at).toLocaleDateString('zh-CN')}
                  </p>
                </div>
                <a
                  href={api.finalVideoUrl(id)}
                  download={`${currentProject.title}.mp4`}
                  className="inline-flex"
                >
                  <Button>
                    <Download size={14} className="mr-2" />
                    下载 MP4
                  </Button>
                </a>
              </div>
            </div>

            <div className="flex gap-3">
              <Button
                variant="outline"
                onClick={() => router.push(`/projects/${id}/shots`)}
              >
                退回分镜审批
              </Button>
              <Button
                variant="outline"
                onClick={() => router.push(`/projects/${id}/script`)}
              >
                退回脚本审批
              </Button>
            </div>
          </div>
        )}
      </main>
    </div>
  )
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend && npx tsc --noEmit
```
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add "frontend/app/projects/[id]/export/"
git commit -m "feat: add export page with video player and download"
```

---

## Task 18: Dockerfile + .env.local

**Files:**
- Create: `frontend/Dockerfile`

- [ ] **Step 1: Write Dockerfile**

```dockerfile
# frontend/Dockerfile
FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
RUN addgroup --system --gid 1001 nodejs && adduser --system --uid 1001 nextjs
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
EXPOSE 3000
ENV PORT=3000
CMD ["node", "server.js"]
```

- [ ] **Step 2: Enable standalone output in next.config**

Edit `frontend/next.config.ts` (or `next.config.mjs`) — add `output: 'standalone'`:

```ts
import type { NextConfig } from 'next'

const nextConfig: NextConfig = {
  output: 'standalone',
}

export default nextConfig
```

- [ ] **Step 3: Verify .env.local exists**

```bash
cat frontend/.env.local
```
Expected output: `NEXT_PUBLIC_API_BASE=http://localhost:8000`

If missing, create it:
```bash
echo "NEXT_PUBLIC_API_BASE=http://localhost:8000" > frontend/.env.local
```

- [ ] **Step 4: Final type-check and build**

```bash
cd frontend && npx tsc --noEmit && npm run build
```
Expected: TypeScript clean, Next.js build succeeds.

- [ ] **Step 5: Commit**

```bash
git add frontend/Dockerfile frontend/next.config.ts
git commit -m "feat: add Dockerfile with multi-stage build"
```

---

## Self-Review

**Spec coverage:**
- ✅ All routes from ARCHTECH §3.1
- ✅ Status router logic (§3.2)
- ✅ All lib layers (§5, §6, §7, §8)
- ✅ All 4 components (§4)
- ✅ SSE event → store mapping (§5.2)
- ✅ computeCascadeWarnings (§5.4)
- ✅ Cascade warning UI with "一键追加" (§5.4)
- ✅ 60s SSE timeout fallback (§4.3)
- ✅ FAILED state handling (§10.3)
- ✅ Dockerfile (§2)

**Placeholder scan:** No TBD/TODO in any task. All code steps include complete implementations.

**Type consistency:**
- `Shot.shot_id` used consistently in `updateShot`, `ShotCard`, `computeCascadeWarnings`
- `api.patchStoryboard` uses `Partial<Pick<Storyboard, 'scene_overview'>>` — matches usage in script page
- `SSEEventType` used in `sse.ts` subscriber list matches all 10 types in `types.ts`
