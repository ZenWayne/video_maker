// components/ProgressStream.tsx - SSE 进度订阅组件

'use client'

import { useEffect, useRef, useState, useCallback } from 'react'
import { createSSEConnection, type SSEConnection } from '@/lib/sse'
import { Progress } from '@/components/ui/progress'
import { useStore } from '@/lib/state'
import type {
  SSEEventType,
  StateSnapshotData,
  ScriptReadyData,
  ShotProgressData,
  ShotCompletedData,
  ShotFailedData,
  ShotReviewReadyData,
  ExportDoneData,
  PipelineFailedData,
  WorkerStatusData,
} from '@/lib/types'

interface ProgressStreamProps {
  projectId: string
  onEvent?: (type: SSEEventType, data: unknown) => void
}

export function ProgressStream({ projectId, onEvent }: ProgressStreamProps) {
  const { setCurrentProject, setShots, updateProjectStatus, updateShot, addToast } = useStore()
  const [progress, setProgress] = useState(0)
  const [status, setStatus] = useState('连接中...')
  const [lastEventTime, setLastEventTime] = useState(Date.now())
  const sseRef = useRef<SSEConnection | null>(null)

  // 检查长时间无事件
  useEffect(() => {
    const interval = setInterval(() => {
      const elapsed = Date.now() - lastEventTime
      if (elapsed > 60000) {
        setStatus('检查服务器状态...')
        // 触发父组件刷新
        onEvent?.('state_change', { check: true })
      }
    }, 10000)

    return () => clearInterval(interval)
  }, [lastEventTime, onEvent])

  // 计算总体进度
  const calculateProgress = useCallback((shots: { status: string }[]) => {
    if (shots.length === 0) return 0
    const completed = shots.filter((s) => s.status === 'completed').length
    return Math.round((completed / shots.length) * 100)
  }, [])

  useEffect(() => {
    const sse = createSSEConnection(projectId)
    sseRef.current = sse

    // state_snapshot - 初始化状态
    const unsubscribeSnapshot = sse.subscribe('state_snapshot', (data) => {
      setLastEventTime(Date.now())
      const snapshot = data as StateSnapshotData
      setCurrentProject(snapshot.project)
      setShots(snapshot.shots)
      setProgress(calculateProgress(snapshot.shots))
      setStatus(`当前状态: ${snapshot.project.status}`)
    })

    // state_change - 状态变更
    const unsubscribeStateChange = sse.subscribe('state_change', (data) => {
      setLastEventTime(Date.now())
      const status = (data as { status: string }).status
      updateProjectStatus(status as 'scripting' | 'shot_generating' | 'shot_review' | 'exporting' | 'exported' | 'failed')
      setStatus(`状态更新: ${status}`)
      onEvent?.('state_change', data)
    })

    // script_ready - 脚本生成完成
    const unsubscribeScriptReady = sse.subscribe('script_ready', (data) => {
      setLastEventTime(Date.now())
      const scriptData = data as ScriptReadyData
      setShots(scriptData.storyboard.shots)
      updateProjectStatus('script_review')
      setStatus('脚本生成完成')
      addToast({ type: 'success', message: '脚本已生成，请审阅' })
      onEvent?.('script_ready', data)
    })

    // shot_started - 分镜开始生成
    const unsubscribeShotStarted = sse.subscribe('shot_started', (data) => {
      setLastEventTime(Date.now())
      const shotId = (data as { shot_id: number }).shot_id
      updateShot(shotId, { status: 'prompt_generating' })
      setStatus(`分镜 #${shotId} 开始生成`)
    })

    // shot_progress - 分镜进度更新
    const unsubscribeShotProgress = sse.subscribe('shot_progress', (data) => {
      setLastEventTime(Date.now())
      const progressData = data as ShotProgressData
      updateShot(progressData.shot_id, { status: progressData.sub_status })
    })

    // shot_completed - 分镜完成
    const unsubscribeShotCompleted = sse.subscribe('shot_completed', (data) => {
      setLastEventTime(Date.now())
      const completedData = data as ShotCompletedData
      updateShot(completedData.shot_id, {
        status: 'completed',
        video_path: completedData.video_path,
        last_frame_path: completedData.last_frame_path,
      })
      // 重新计算进度
      const currentShots = useStore.getState().shots
      setProgress(calculateProgress(currentShots))
      setStatus(`分镜 #${completedData.shot_id} 完成`)
    })

    // shot_failed - 分镜失败
    const unsubscribeShotFailed = sse.subscribe('shot_failed', (data) => {
      setLastEventTime(Date.now())
      const failedData = data as ShotFailedData
      updateShot(failedData.shot_id, {
        status: 'failed',
        error_message: failedData.error_message,
      })
      addToast({
        type: 'error',
        message: `分镜 #${failedData.shot_id} 生成失败: ${failedData.error_message}`,
      })
    })

    // all_shots_ready - 所有分镜就绪
    const unsubscribeAllShotsReady = sse.subscribe('all_shots_ready', () => {
      setLastEventTime(Date.now())
      updateProjectStatus('shot_review')
      setProgress(100)
      setStatus('所有分镜生成完成')
      addToast({ type: 'success', message: '所有分镜已生成，请审阅' })
      onEvent?.('all_shots_ready', {})
    })

    // shot_review_ready - 单个分镜完成，等待审阅
    const unsubscribeShotReviewReady = sse.subscribe('shot_review_ready', (data) => {
      setLastEventTime(Date.now())
      const d = data as ShotReviewReadyData
      updateProjectStatus('shot_review')
      setProgress(Math.round((d.completed / d.total) * 100))
      setStatus(`镜头 ${d.completed}/${d.total} 已完成，等待审阅`)
      addToast({ type: 'success', message: `镜头 ${d.completed}/${d.total} 已完成，请审阅` })
      onEvent?.('shot_review_ready', data)
    })

    // export_done - 导出完成
    const unsubscribeExportDone = sse.subscribe('export_done', (data) => {
      setLastEventTime(Date.now())
      const exportData = data as ExportDoneData
      updateProjectStatus('exported')
      setStatus('视频导出完成')
      addToast({ type: 'success', message: '视频导出完成！' })
      onEvent?.('export_done', exportData)
    })

    // worker_status - 重试/进度状态
    const unsubscribeWorkerStatus = sse.subscribe('worker_status', (data) => {
      setLastEventTime(Date.now())
      const statusData = data as WorkerStatusData
      setStatus(statusData.message)
    })

    // pipeline_failed - Pipeline 失败
    const unsubscribePipelineFailed = sse.subscribe('pipeline_failed', (data) => {
      setLastEventTime(Date.now())
      const failedData = data as PipelineFailedData
      updateProjectStatus('failed')
      setStatus(`Pipeline 失败: ${failedData.error_message}`)
      addToast({ type: 'error', message: failedData.error_message })
    })

    return () => {
      unsubscribeSnapshot()
      unsubscribeStateChange()
      unsubscribeScriptReady()
      unsubscribeShotStarted()
      unsubscribeShotProgress()
      unsubscribeShotCompleted()
      unsubscribeShotFailed()
      unsubscribeAllShotsReady()
      unsubscribeShotReviewReady()
      unsubscribeExportDone()
      unsubscribeWorkerStatus()
      unsubscribePipelineFailed()
      sse.close()
    }
  }, [projectId, setCurrentProject, setShots, updateProjectStatus, updateShot, addToast, onEvent, calculateProgress])

  return (
    <div className="space-y-2 p-4 bg-zinc-50 rounded-lg">
      <div className="flex items-center justify-between text-sm">
        <span className="text-zinc-600">{status}</span>
        <span className="text-zinc-500">{progress}%</span>
      </div>
      <Progress value={progress} className="h-2" />
    </div>
  )
}
