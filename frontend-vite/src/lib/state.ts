// lib/state.ts - Zustand 全局状态管理

import { create } from 'zustand'
import type {
  Project,
  ProjectStatus,
  Shot,
  Toast,
} from './types'

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
  addToast: (toast: Omit<Toast, 'id'>) => void
  removeToast: (id: string) => void
}

export const useStore = create<AppStore>((set, get) => ({
  // 用户名
  userName: '',
  setUserName: (name) => {
    if (typeof window !== 'undefined') {
      localStorage.setItem('user_name', name)
    }
    set({ userName: name })
  },

  // 当前项目
  currentProject: null,
  setCurrentProject: (project) => set({ currentProject: project }),
  updateProjectStatus: (status) =>
    set((state) => ({
      currentProject: state.currentProject
        ? { ...state.currentProject, status }
        : null,
    })),

  // Shots
  shots: [],
  setShots: (shots) => set({ shots }),
  updateShot: (shotId, patch) =>
    set((state) => ({
      shots: state.shots.map((s) =>
        s.shot_id === shotId ? { ...s, ...patch } : s
      ),
    })),

  // 多选状态
  selectedShotIds: new Set(),
  toggleShotSelection: (shotId) =>
    set((state) => {
      const newSet = new Set(state.selectedShotIds)
      if (newSet.has(shotId)) {
        newSet.delete(shotId)
      } else {
        newSet.add(shotId)
      }
      return { selectedShotIds: newSet }
    }),
  clearSelection: () => set({ selectedShotIds: new Set() }),

  // Toast
  toasts: [],
  addToast: (toast) => {
    const id = Math.random().toString(36).substring(2, 9)
    set((state) => ({
      toasts: [...state.toasts, { ...toast, id }],
    }))
    // 自动移除
    setTimeout(() => {
      get().removeToast(id)
    }, 5000)
  },
  removeToast: (id) =>
    set((state) => ({
      toasts: state.toasts.filter((t) => t.id !== id),
    })),
}))

// 初始化用户名（仅在客户端）
if (typeof window !== 'undefined') {
  const storedName = localStorage.getItem('user_name') || ''
  useStore.setState({ userName: storedName })
}
