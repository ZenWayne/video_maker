// lib/analysisStatus.ts - 分析状态映射 + 简报解析

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

export type StepState = 'done' | 'active' | 'pending'

// 三步进度器：转写 → 归纳 → 完成。返回每步的 done/active/pending。
export function analysisSteps(status: ContentAnalysisStatus): { key: string; label: string; state: StepState }[] {
  const order: ContentAnalysisStatus[] = ['transcribing', 'analyzing', 'completed']
  const idx = status === 'failed' ? -1 : order.indexOf(status === 'uploading' ? 'transcribing' : status)
  return [
    { key: 'transcribing', label: '转写口播' },
    { key: 'analyzing', label: '联合归纳' },
    { key: 'completed', label: '完成' },
  ].map((s, i): { key: string; label: string; state: StepState } => ({
    ...s,
    state: idx < 0 ? 'pending' : i < idx ? 'done' : i === idx ? 'active' : 'pending',
  }))
}
