// lib/api.ts - 后端 REST API 封装

import type {
  Project,
  ProjectDetail,
  ProjectStatus,
  ReferenceImageKind,
  APIError,
} from './types'

const BASE = import.meta.env.VITE_API_BASE || ''

class APIErrorClass extends Error {
  code: string

  constructor(error: APIError) {
    super(error.message)
    this.code = error.code
    this.name = 'APIError'
  }
}

function getUserName(): string {
  if (typeof window === 'undefined') return 'anonymous'
  const name = localStorage.getItem('user_name')
  if (!name) {
    localStorage.setItem('user_name', 'anonymous')
    return 'anonymous'
  }
  return name
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown
): Promise<T> {
  const url = `${BASE}${path}`
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  }

  const userName = getUserName()
  if (userName) {
    headers['X-User-Name'] = userName
  }

  const options: RequestInit = {
    method,
    headers,
  }

  if (body !== undefined) {
    options.body = JSON.stringify(body)
  }

  const response = await fetch(url, options)

  if (!response.ok) {
    let errorData: { error?: APIError; detail?: string }
    try {
      errorData = await response.json()
    } catch {
      throw new APIErrorClass({
        code: 'UNKNOWN_ERROR',
        message: `HTTP ${response.status}: ${response.statusText}`,
      })
    }
    const apiError: APIError = errorData.error
      ?? (errorData.detail ? { code: 'API_ERROR', message: errorData.detail } : null)
      ?? { code: 'UNKNOWN_ERROR', message: 'Unknown error' }
    throw new APIErrorClass(apiError)
  }

  if (response.status === 204) {
    return undefined as T
  }

  return response.json()
}

// 单文件 multipart 上传（字段名固定为 `file`）
async function uploadSingle<T>(path: string, file: File): Promise<T> {
  const formData = new FormData()
  formData.append('file', file)
  const headers: Record<string, string> = {}
  const userName = getUserName()
  if (userName) headers['X-User-Name'] = userName

  const response = await fetch(`${BASE}${path}`, { method: 'POST', headers, body: formData })
  if (!response.ok) {
    let errorData: { error?: APIError; detail?: string }
    try { errorData = await response.json() } catch {
      throw new APIErrorClass({ code: 'UPLOAD_ERROR', message: `Upload failed: ${response.status}` })
    }
    throw new APIErrorClass(
      errorData.error ?? (errorData.detail ? { code: 'UPLOAD_ERROR', message: errorData.detail } : { code: 'UPLOAD_ERROR', message: 'Upload failed' })
    )
  }
  return response.json()
}

// 项目管理
export const api = {
  // 获取项目列表
  listProjects: (params?: {
    status?: string
    creator?: string
    sort?: string
  }): Promise<Project[]> => {
    const searchParams = new URLSearchParams()
    if (params?.status) searchParams.set('status', params.status)
    if (params?.creator) searchParams.set('creator', params.creator)
    if (params?.sort) searchParams.set('sort', params.sort)
    const query = searchParams.toString()
    return request<{ items: Project[]; total: number; limit: number; offset: number }>(
      'GET', `/api/projects${query ? `?${query}` : ''}`
    ).then((data) => data.items)
  },

  // 创建项目
  createProject: (data: {
    title: string
    theme_text: string
    aspect_ratio?: '16:9' | '9:16'
  }): Promise<{ project_id: string; status: ProjectStatus }> => {
    return request<{ id: string; status: ProjectStatus }>('POST', '/api/projects', data)
      .then((r) => ({ project_id: r.id, status: r.status }))
  },

  // 获取项目详情
  getProject: (id: string): Promise<ProjectDetail> => {
    return request('GET', `/api/projects/${id}`)
  },

  // 删除项目
  deleteProject: (id: string): Promise<void> => {
    return request('DELETE', `/api/projects/${id}`)
  },

  // 上传参考图
  uploadReferenceImages: async (
    id: string,
    files: File[],
    kind: ReferenceImageKind
  ): Promise<{ image_ids: string[] }> => {
    const formData = new FormData()
    files.forEach((file) => formData.append('files', file))
    formData.append('kind', kind)

    const url = `${BASE}/api/projects/${id}/reference-images`
    const headers: Record<string, string> = {}

    const userName = getUserName()
    if (userName) {
      headers['X-User-Name'] = userName
    }

    const response = await fetch(url, {
      method: 'POST',
      headers,
      body: formData,
    })

    if (!response.ok) {
      let errorData: { error?: APIError }
      try {
        errorData = await response.json()
      } catch {
        throw new APIErrorClass({
          code: 'UPLOAD_ERROR',
          message: `Failed to upload images: ${response.status}`,
        })
      }
      throw new APIErrorClass(errorData.error || { code: 'UPLOAD_ERROR', message: 'Upload failed' })
    }

    return response.json()
  },

  // 删除参考图
  deleteReferenceImage: (id: string, imageId: string): Promise<void> => {
    return request('DELETE', `/api/projects/${id}/reference-images/${imageId}`)
  },

  // 启动 pipeline
  startPipeline: (id: string): Promise<void> => {
    return request('POST', `/api/projects/${id}/start`)
  },

  // 重新生成脚本
  regenerateScript: (id: string): Promise<void> => {
    return request('POST', `/api/projects/${id}/regenerate-script`)
  },

  // 更新 storyboard
  patchStoryboard: (
    id: string,
    data: { scene_overview?: string }
  ): Promise<void> => {
    return request('PATCH', `/api/projects/${id}/storyboard`, data)
  },

  // 通过脚本审批
  approveScript: (id: string): Promise<void> => {
    return request('POST', `/api/projects/${id}/approve-script`)
  },

  // 重新生成分镜
  regenerateShots: (id: string, shotIds: number[]): Promise<void> => {
    return request('POST', `/api/projects/${id}/regenerate-shots`, {
      shot_ids: shotIds,
    })
  },

  // 继续生成下一个镜头
  continueGeneration: (id: string): Promise<void> => {
    return request('POST', `/api/projects/${id}/continue-generation`)
  },

  // 更新 shot
  patchShot: (
    id: string,
    shotId: number,
    data: { text?: string; visual_description?: string; motion_prompt?: string; align_with_previous?: boolean; use_prev_last_frame?: boolean; shot_duration?: number; auto_trim?: boolean }
  ): Promise<void> => {
    return request('PATCH', `/api/projects/${id}/shots/${shotId}`, data)
  },

  // 导出视频
  exportVideo: (id: string): Promise<void> => {
    return request('POST', `/api/projects/${id}/export`)
  },

  // 临时拼接选中镜头，用于检测连贯性
  joinPreview: (
    id: string,
    shotIds: number[]
  ): Promise<{ preview_url: string }> => {
    return request('POST', `/api/projects/${id}/join-preview`, {
      shot_ids: shotIds,
    })
  },

  // AI 编辑运镜提示词
  aiEditPrompt: (
    projectId: string,
    shotId: number,
    instruction: string
  ): Promise<{ motion_prompt: string }> => {
    return request('POST', `/api/projects/${projectId}/shots/${shotId}/ai-edit-prompt`, { instruction })
  },

  // 重写运镜提示词（Director 重新生成）
  rewritePrompt: (
    projectId: string,
    shotId: number,
  ): Promise<{ motion_prompt: string }> => {
    return request('POST', `/api/projects/${projectId}/shots/${shotId}/rewrite-prompt`)
  },

  // AI 编辑分镜
  aiEditShot: (
    projectId: string,
    shotId: number,
    instruction: string
  ): Promise<{ text: string; visual_description: string }> => {
    return request('POST', `/api/projects/${projectId}/shots/${shotId}/ai-edit`, { instruction })
  },

  // 取消生成（shot_generating -> shot_review）
  cancelGeneration: (id: string): Promise<void> => {
    return request('POST', `/api/projects/${id}/cancel-generation`)
  },

  // 重置到脚本阶段
  resetToScript: (id: string): Promise<void> => {
    return request('POST', `/api/projects/${id}/reset-to-script`)
  },

  // 重置项目
  resetProject: (id: string): Promise<void> => {
    return request('POST', `/api/projects/${id}/reset`)
  },

  // 上传 shot 参考图（断开分镜用）
  uploadShotReferences: async (
    projectId: string,
    shotId: number,
    files: File[]
  ): Promise<{ shot_id: number; custom_first_frame_path: string | null; custom_reference_paths: string[] | null }> => {
    const formData = new FormData()
    files.forEach((file) => formData.append('files', file))

    const url = `${BASE}/api/projects/${projectId}/shots/${shotId}/reference-images`
    const headers: Record<string, string> = {}
    const userName = getUserName()
    if (userName) headers['X-User-Name'] = userName

    const response = await fetch(url, { method: 'POST', headers, body: formData })
    if (!response.ok) {
      let errorData: { error?: APIError }
      try { errorData = await response.json() } catch {
        throw new APIErrorClass({ code: 'UPLOAD_ERROR', message: `Upload failed: ${response.status}` })
      }
      throw new APIErrorClass(errorData.error || { code: 'UPLOAD_ERROR', message: 'Upload failed' })
    }
    return response.json()
  },

  // 删除 shot 参考图（index 为空则全删）
  deleteShotReference: (
    projectId: string,
    shotId: number,
    index?: number
  ): Promise<{ shot_id: number; custom_first_frame_path: string | null; custom_reference_paths: string[] | null }> => {
    const query = index !== undefined ? `?index=${index}` : ''
    return request('DELETE', `/api/projects/${projectId}/shots/${shotId}/reference-images${query}`)
  },

  // 参考图排序
  reorderShotReferences: (
    projectId: string,
    shotId: number,
    order: number[]
  ): Promise<{ shot_id: number; custom_first_frame_path: string | null; custom_reference_paths: string[] | null }> => {
    return request('PUT', `/api/projects/${projectId}/shots/${shotId}/reference-images/reorder`, { order })
  },

  // 视频元信息
  getVideoInfo: (projectId: string, shotId: number): Promise<{ fps: number; total_frames: number; duration: number; has_backup: boolean }> => {
    return request('GET', `/api/projects/${projectId}/shots/${shotId}/video-info`)
  },

  // 裁剪视频
  trimShot: (
    projectId: string,
    shotId: number,
    endFrame: number
  ): Promise<{ video_path: string; last_frame_path: string; version: number; fps: number; total_frames: number; duration: number }> => {
    return request('POST', `/api/projects/${projectId}/shots/${shotId}/trim`, { end_frame: endFrame })
  },

  // 还原裁剪
  restoreTrim: (
    projectId: string,
    shotId: number
  ): Promise<{ video_path: string; last_frame_path: string; version: number; fps: number; total_frames: number; duration: number }> => {
    return request('POST', `/api/projects/${projectId}/shots/${shotId}/restore-trim`)
  },

  // 智能尾帧校准
  alignTailFrame: (
    projectId: string,
    shotId: number
  ): Promise<{ video_path: string; last_frame_path: string; version: number; fps: number; total_frames: number; duration: number; aligned_to_frame: number }> => {
    return request('POST', `/api/projects/${projectId}/shots/${shotId}/align-tail-frame`)
  },

  // 静音裁剪建议（只读，返回建议帧，不实际裁剪）
  detectSilence: (
    projectId: string,
    shotId: number
  ): Promise<{ has_silence: boolean; suggested_end_frame: number | null; silence_start_time: number | null; fps: number; total_frames: number; duration: number }> => {
    return request('POST', `/api/projects/${projectId}/shots/${shotId}/detect-silence`)
  },

  // 设置基准音色
  setReferenceVoice: (projectId: string, shotId: number): Promise<{ reference_voice_shot_id: number }> => {
    return request('POST', `/api/projects/${projectId}/reference-voice`, { shot_id: shotId })
  },

  // 清除基准音色
  clearReferenceVoice: (projectId: string): Promise<void> => {
    return request('DELETE', `/api/projects/${projectId}/reference-voice`)
  },

  // 上传基准音色文件 (mp4/m4a/wav)
  uploadReferenceVoice: async (
    projectId: string,
    file: File,
  ): Promise<{ reference_voice_path: string | null; reference_voice_shot_id: number | null }> => {
    const form = new FormData()
    form.append('file', file)
    const res = await fetch(`${BASE}/api/projects/${projectId}/reference-voice/upload`, {
      method: 'POST',
      headers: { 'X-User-Name': getUserName() },
      body: form,
    })
    if (!res.ok) throw new Error(`Upload failed: ${res.status}`)
    return res.json()
  },

  // 自动音色校准开关
  setAutoVoiceCalibrate: (
    projectId: string,
    enabled: boolean,
  ): Promise<{ auto_voice_calibrate: boolean }> => {
    return request('POST', `/api/projects/${projectId}/auto-voice-calibrate`, { enabled })
  },

  // 单个 shot 音色转换
  voiceConvert: (projectId: string, shotId: number): Promise<void> => {
    return request('POST', `/api/projects/${projectId}/shots/${shotId}/voice-convert`)
  },

  // 批量音色转换
  voiceConvertAll: (projectId: string): Promise<{ shot_ids: number[] }> => {
    return request('POST', `/api/projects/${projectId}/voice-convert-all`)
  },

  // 还原音色
  voiceRevert: (projectId: string, shotId: number): Promise<{ video_path: string; version: number }> => {
    return request('POST', `/api/projects/${projectId}/shots/${shotId}/voice-revert`)
  },

  // 单个 shot 人物校准
  characterCalibrate: (projectId: string, shotId: number): Promise<void> => {
    return request('POST', `/api/projects/${projectId}/shots/${shotId}/character-calibrate`)
  },

  // 批量人物校准
  characterCalibrateAll: (projectId: string): Promise<{ shot_ids: number[] }> => {
    return request('POST', `/api/projects/${projectId}/character-calibrate-all`)
  },

  // 生成目标尾帧
  generateTailFrame: (projectId: string, shotId: number): Promise<void> => {
    return request('POST', `/api/projects/${projectId}/shots/${shotId}/generate-tail-frame`)
  },

  // 从视频提取尾帧
  extractTailFrame: (projectId: string, shotId: number): Promise<{ target_last_frame_path: string }> => {
    return request('POST', `/api/projects/${projectId}/shots/${shotId}/extract-tail-frame`)
  },

  // 确认尾帧
  confirmTailFrame: (projectId: string, shotId: number): Promise<{ tf_confirmed: boolean; target_last_frame_path: string }> => {
    return request('POST', `/api/projects/${projectId}/shots/${shotId}/confirm-tail-frame`)
  },

  // 删除尾帧（清空 target_last_frame_path + tf_status，path-as-truth）
  deleteTailFrame: (projectId: string, shotId: number): Promise<{ shot_id: number; target_last_frame_path: null; tf_status: null }> => {
    return request('POST', `/api/projects/${projectId}/shots/${shotId}/delete-tail-frame`)
  },

  // ── 关键帧管理（首帧 = custom_first_frame_path，尾帧 = target_last_frame_path）──

  // 上传首帧（ts_uuid 命名）
  uploadFirstFrame: (projectId: string, shotId: number, file: File): Promise<{ shot_id: number; custom_first_frame_path: string }> => {
    return uploadSingle(`/api/projects/${projectId}/shots/${shotId}/upload-first-frame`, file)
  },

  // 上传尾帧（ts_uuid 命名，写 tf_status=done）
  uploadTailFrame: (projectId: string, shotId: number, file: File): Promise<{ shot_id: number; target_last_frame_path: string; tf_status: string }> => {
    return uploadSingle(`/api/projects/${projectId}/shots/${shotId}/upload-tail-frame`, file)
  },

  // 提取本镜首帧 → 首帧配置
  extractFirstFrame: (projectId: string, shotId: number): Promise<{ shot_id: number; custom_first_frame_path: string }> => {
    return request('POST', `/api/projects/${projectId}/shots/${shotId}/extract-first-frame`)
  },

  // 提取本镜尾帧 → 尾帧配置
  extractLastFrame: (projectId: string, shotId: number): Promise<{ shot_id: number; target_last_frame_path: string; tf_status: string }> => {
    return request('POST', `/api/projects/${projectId}/shots/${shotId}/extract-last-frame`)
  },

  // 删除首帧（清空 custom_first_frame_path + unlink）
  deleteFirstFrame: (projectId: string, shotId: number): Promise<{ shot_id: number; custom_first_frame_path: null }> => {
    return request('DELETE', `/api/projects/${projectId}/shots/${shotId}/first-frame`)
  },

  // 还原人物校准
  characterCalibrateRevert: (projectId: string, shotId: number): Promise<{ last_frame_path: string; version: number }> => {
    return request('POST', `/api/projects/${projectId}/shots/${shotId}/character-calibrate-revert`)
  },

  // 资源 URL
  assetUrl: (projectId: string, kind: string, file: string): string => {
    return `${BASE}/api/projects/${projectId}/assets/${kind}/${file}`
  },

  // 成片视频 URL
  finalVideoUrl: (id: string): string => {
    return `${BASE}/api/projects/${id}/final-video`
  },
}

export { APIErrorClass }
export type { APIError }
