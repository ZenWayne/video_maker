// lib/analysisSse.ts - EventSource 封装（镜像 lib/sse.ts，仅把 URL 指向 analysis stream）

// 分析流的事件集合与项目流（SSEEventType）不同：连接时的 state_snapshot 之外，
// 只有 worker 发布的 analysis_progress（见 backend/worker/tasks.py::run_content_analysis）。
export type AnalysisSSEEventType = 'state_snapshot' | 'analysis_progress'

export interface AnalysisSSEConnection {
  subscribe(event: AnalysisSSEEventType, handler: (data: unknown) => void): () => void
  close(): void
}

// 向后兼容别名（与 lib/sse.ts 的 SSEConnection 同形状）
export type SSEConnection = AnalysisSSEConnection

// sse.ts 未导出 BASE，此处本地重定义（不 import 私有符号）
const BASE = import.meta.env.VITE_API_BASE || ''

interface AnalysisSSEEvent {
  type: AnalysisSSEEventType
  data: unknown
}

export function createAnalysisSSEConnection(analysisId: string): AnalysisSSEConnection {
  const url = `${BASE}/api/analyses/${analysisId}/stream`
  const eventSource = new EventSource(url)
  const handlers = new Map<AnalysisSSEEventType, Set<(data: unknown) => void>>()

  eventSource.onmessage = (event) => {
    try {
      const parsed: AnalysisSSEEvent = JSON.parse(event.data)
      const eventHandlers = handlers.get(parsed.type)
      if (eventHandlers) {
        eventHandlers.forEach((handler) => handler(parsed.data))
      }
    } catch (error) {
      console.error('Failed to parse SSE message:', error)
    }
  }

  eventSource.onerror = (error) => {
    console.error('SSE connection error:', error)
  }

  return {
    subscribe(event: AnalysisSSEEventType, handler: (data: unknown) => void) {
      if (!handlers.has(event)) {
        handlers.set(event, new Set())
      }
      handlers.get(event)!.add(handler)

      // 返回取消订阅函数
      return () => {
        handlers.get(event)?.delete(handler)
      }
    },

    close() {
      eventSource.close()
    },
  }
}
