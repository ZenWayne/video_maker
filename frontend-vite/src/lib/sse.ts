// lib/sse.ts - EventSource 封装

import type { SSEEventType, SSEEvent } from './types'

export interface SSEConnection {
  subscribe(event: SSEEventType, handler: (data: unknown) => void): () => void
  close(): void
}

const BASE = import.meta.env.VITE_API_BASE || ''

export function createSSEConnection(projectId: string): SSEConnection {
  const url = `${BASE}/api/projects/${projectId}/stream`
  // withCredentials 是 SSE 唯一能带上会话的方式：EventSource 不支持自定义
  // 请求头（Web 标准限制），所以 Authorization: Bearer 这类方案覆盖不到进度流。
  const eventSource = new EventSource(url, { withCredentials: true })
  const handlers = new Map<SSEEventType, Set<(data: unknown) => void>>()

  eventSource.onmessage = (event) => {
    try {
      const parsed: SSEEvent = JSON.parse(event.data)
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
    subscribe(event: SSEEventType, handler: (data: unknown) => void) {
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
