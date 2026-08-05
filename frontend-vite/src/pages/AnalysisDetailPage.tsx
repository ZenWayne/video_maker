// pages/AnalysisDetailPage.tsx - 内容分析详情页（SSE 进度 + 简报展示）

import { useEffect, useRef, useState, useCallback } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { AnalysisProgress } from '@/components/AnalysisProgress'
import { BriefView } from '@/components/BriefView'
import { createAnalysisSSEConnection } from '@/lib/analysisSse'
import type { SSEConnection } from '@/lib/analysisSse'
import { api } from '@/lib/api'
import { useStore } from '@/lib/state'
import { ANALYSIS_STATUS_LABELS, ANALYSIS_STATUS_COLORS, parseBrief } from '@/lib/analysisStatus'
import type { ContentAnalysis } from '@/lib/types'

const STALL_MS = 30000
const POLL_INTERVAL_MS = 10000

export default function AnalysisDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const { addToast } = useStore()
  const [analysis, setAnalysis] = useState<ContentAnalysis | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const lastEventTimeRef = useRef(Date.now())
  const sseRef = useRef<SSEConnection | null>(null)

  const refresh = useCallback(async () => {
    if (!id) return
    try {
      const a = await api.getAnalysis(id)
      setAnalysis(a)
      lastEventTimeRef.current = Date.now()
    } catch {
      // ignore transient fetch errors — SSE / next poll will retry
    }
  }, [id])

  // 初次加载
  useEffect(() => {
    if (!id) return
    let cancelled = false
    setIsLoading(true)
    api
      .getAnalysis(id)
      .then((a) => {
        if (cancelled) return
        setAnalysis(a)
      })
      .catch((error) => {
        if (cancelled) return
        addToast({
          type: 'error',
          message: error instanceof Error ? error.message : '加载分析详情失败',
        })
      })
      .finally(() => {
        if (cancelled) return
        setIsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [id, addToast])

  // SSE 订阅
  useEffect(() => {
    if (!id) return
    const sse = createAnalysisSSEConnection(id)
    sseRef.current = sse

    const unsubscribeSnapshot = sse.subscribe('state_snapshot', (data) => {
      lastEventTimeRef.current = Date.now()
      setAnalysis(data as ContentAnalysis)
    })

    // 不假设细粒度事件形状（如 analysis_progress），任意进度事件都重新拉取完整状态
    const unsubscribeProgress = sse.subscribe('analysis_progress', () => {
      lastEventTimeRef.current = Date.now()
      refresh()
    })

    return () => {
      unsubscribeSnapshot()
      unsubscribeProgress()
      sse.close()
    }
  }, [id, refresh])

  // 停滞兜底：进行中状态下超过 30s 无事件则重新拉取
  useEffect(() => {
    const interval = setInterval(() => {
      const elapsed = Date.now() - lastEventTimeRef.current
      if (elapsed < STALL_MS) return
      const status = analysis?.status
      if (status !== 'transcribing' && status !== 'analyzing') return
      refresh()
    }, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [analysis?.status, refresh])

  const handleAttach = useCallback(() => {
    addToast({ type: 'info', message: '请在新建项目时挂载该简报' })
  }, [addToast])

  if (isLoading) {
    return (
      <div className="min-h-screen bg-zinc-50 flex items-center justify-center">
        <div className="w-8 h-8 border-4 border-blue-500 border-t-transparent rounded-full animate-spin" />
      </div>
    )
  }

  if (!analysis) {
    return (
      <div className="min-h-screen bg-zinc-50 flex flex-col items-center justify-center gap-4">
        <p className="text-zinc-500">分析不存在</p>
        <Button variant="outline" onClick={() => navigate('/analyses')}>
          返回列表
        </Button>
      </div>
    )
  }

  const brief = analysis.status === 'completed' ? parseBrief(analysis.brief_json) : null

  return (
    <div data-testid="analysis-detail-page" className="min-h-screen bg-zinc-50">
      <header className="bg-white border-b sticky top-0 z-10">
        <div className="max-w-3xl mx-auto px-4 sm:px-6 h-16 flex items-center gap-3">
          <Button variant="ghost" size="icon" onClick={() => navigate('/analyses')}>
            <ArrowLeft className="w-4 h-4" />
          </Button>
          <h1 className="text-xl font-semibold text-zinc-900 flex-1 min-w-0 truncate">
            {analysis.title}
          </h1>
          <Badge variant="secondary" className={ANALYSIS_STATUS_COLORS[analysis.status]}>
            {ANALYSIS_STATUS_LABELS[analysis.status]}
          </Badge>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-4 sm:px-6 py-8 space-y-4">
        {analysis.status === 'failed' && analysis.error_message && (
          <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
            {analysis.error_message}
          </div>
        )}

        {analysis.status === 'completed' && brief ? (
          <BriefView brief={brief} onAttach={handleAttach} />
        ) : analysis.status === 'completed' && !brief ? (
          <div
            data-testid="brief-parse-error"
            className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700 space-y-1"
          >
            <p>简报解析失败</p>
            {analysis.error_message && <p className="text-red-600">{analysis.error_message}</p>}
          </div>
        ) : (
          <AnalysisProgress analysis={analysis} />
        )}
      </main>
    </div>
  )
}
