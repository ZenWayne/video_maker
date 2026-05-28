// components/ProgressStream.tsx - SSE 进度订阅组件

'use client'

import { useEffect, useRef, useState, useCallback } from 'react'
import { createSSEConnection, type SSEConnection } from '@/lib/sse'
import { Progress } from '@/components/ui/progress'
import { useStore } from '@/lib/state'
import { api } from '@/lib/api'
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

  // Fallback: 当有异步操作进行中且 SSE 长时间无事件时，从 API 拉取最新状态
  // 防止 Redis pub/sub 事件丢失导致前端卡住
  useEffect(() => {
    const interval = setInterval(async () => {
      const elapsed = Date.now() - lastEventTime
      if (elapsed < 30000) return

      // 检查是否有进行中的异步操作
      const currentShots = useStore.getState().shots
      const hasPending = currentShots.some(
        (s) =>
          s.vc_status === 'converting' ||
          s.cc_status === 'calibrating' ||
          s.tf_status === 'generating',
      )
      if (!hasPending) return

      try {
        const detail = await api.getProject(projectId)
        setShots(detail.shots)
        setLastEventTime(Date.now())
      } catch {
        // ignore fetch errors
      }
    }, 10000)

    return () => clearInterval(interval)
  }, [projectId, lastEventTime, setShots])

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
        video_path: `${completedData.video_path}?t=${Date.now()}`,
        last_frame_path: `${completedData.last_frame_path}?t=${Date.now()}`,
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

    // tf_started - 尾帧开始生成
    const unsubscribeTfStarted = sse.subscribe('tf_started', (data) => {
      setLastEventTime(Date.now())
      const d = data as { shot_id: number }
      updateShot(d.shot_id, { tf_status: 'generating' } as Partial<{ tf_status: string }>)
      setStatus(`分镜 #${d.shot_id} 尾帧生成中`)
      onEvent?.('tf_started', data)
    })

    // tf_pose_analyzed - 动作分析完成，开始生成尾帧图片
    const unsubscribeTfPoseAnalyzed = sse.subscribe('tf_pose_analyzed', (data) => {
      setLastEventTime(Date.now())
      const d = data as { shot_id: number; end_pose: string }
      setStatus(`分镜 #${d.shot_id} 动作分析完成，正在生成尾帧图片`)
      onEvent?.('tf_pose_analyzed', data)
    })

    // tf_completed - 尾帧生成完成
    const unsubscribeTfCompleted = sse.subscribe('tf_completed', (data) => {
      setLastEventTime(Date.now())
      const d = data as { shot_id: number; target_last_frame_path: string; motion_prompt: string }
      updateShot(d.shot_id, {
        tf_status: 'done',
        target_last_frame_path: `${d.target_last_frame_path}?t=${Date.now()}`,
        motion_prompt: d.motion_prompt,
        tf_confirmed: false,
      } as Partial<{ tf_status: string; target_last_frame_path: string; motion_prompt: string; tf_confirmed: boolean }>)
      updateProjectStatus('shot_review')
      setStatus(`分镜 #${d.shot_id} 尾帧已生成，请确认`)
      addToast({ type: 'success', message: `分镜 #${d.shot_id} 尾帧已生成，请确认` })
      onEvent?.('tf_completed', data)
    })

    // tf_failed - 尾帧生成失败
    const unsubscribeTfFailed = sse.subscribe('tf_failed', (data) => {
      setLastEventTime(Date.now())
      const d = data as { shot_id: number; error_message: string }
      updateShot(d.shot_id, {
        tf_status: 'failed',
        tf_error_message: d.error_message,
      } as Partial<{ tf_status: string; tf_error_message: string }>)
      updateProjectStatus('shot_review')
      setStatus(`分镜 #${d.shot_id} 尾帧生成失败`)
      addToast({ type: 'error', message: `分镜 #${d.shot_id} 尾帧生成失败: ${d.error_message}` })
      onEvent?.('tf_failed', data)
    })

    // vc_started - 音色转换开始
    const unsubscribeVcStarted = sse.subscribe('vc_started', (data) => {
      setLastEventTime(Date.now())
      const d = data as { shot_id: number }
      updateShot(d.shot_id, { vc_status: 'converting' } as any)
      setStatus(`分镜 #${d.shot_id} 音色转换中`)
      onEvent?.('vc_started', data)
    })

    // vc_completed - 音色转换完成
    const unsubscribeVcCompleted = sse.subscribe('vc_completed', (data) => {
      setLastEventTime(Date.now())
      const d = data as { shot_id: number; video_path: string; version?: number }
      const vp = d.version ? `${d.video_path}?v=${d.version}` : d.video_path
      updateShot(d.shot_id, { vc_status: 'done', video_path: vp } as any)
      setStatus(`分镜 #${d.shot_id} 音色转换完成`)
      onEvent?.('vc_completed', data)
    })

    // vc_failed - 音色转换失败
    const unsubscribeVcFailed = sse.subscribe('vc_failed', (data) => {
      setLastEventTime(Date.now())
      const d = data as { shot_id: number; error_message: string }
      updateShot(d.shot_id, { vc_status: 'failed', vc_error_message: d.error_message } as any)
      addToast({ type: 'error', message: `分镜 #${d.shot_id} 音色转换失败` })
      onEvent?.('vc_failed', data)
    })

    // vc_batch_done - 批量音色转换完成
    const unsubscribeVcBatchDone = sse.subscribe('vc_batch_done', (data) => {
      setLastEventTime(Date.now())
      addToast({ type: 'success', message: '批量音色转换完成' })
      onEvent?.('vc_batch_done', data)
    })

    // cc_started - 人物校准开始
    const unsubscribeCcStarted = sse.subscribe('cc_started', (data) => {
      setLastEventTime(Date.now())
      const d = data as { shot_id: number }
      updateShot(d.shot_id, { cc_status: 'calibrating' } as any)
      setStatus(`分镜 #${d.shot_id} 人物校准中`)
      onEvent?.('cc_started', data)
    })

    // cc_completed - 人物校准完成
    const unsubscribeCcCompleted = sse.subscribe('cc_completed', (data) => {
      setLastEventTime(Date.now())
      const d = data as { shot_id: number; last_frame_path: string }
      updateShot(d.shot_id, { cc_status: 'done', last_frame_path: `${d.last_frame_path}?t=${Date.now()}` } as any)
      setStatus(`分镜 #${d.shot_id} 人物校准完成`)
      onEvent?.('cc_completed', data)
    })

    // cc_failed - 人物校准失败
    const unsubscribeCcFailed = sse.subscribe('cc_failed', (data) => {
      setLastEventTime(Date.now())
      const d = data as { shot_id: number; error_message: string }
      updateShot(d.shot_id, { cc_status: 'failed', cc_error_message: d.error_message } as any)
      addToast({ type: 'error', message: `分镜 #${d.shot_id} 人物校准失败` })
      onEvent?.('cc_failed', data)
    })

    // cc_batch_done - 批量人物校准完成
    const unsubscribeCcBatchDone = sse.subscribe('cc_batch_done', (data) => {
      setLastEventTime(Date.now())
      addToast({ type: 'success', message: '批量人物校准完成' })
      onEvent?.('cc_batch_done', data)
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
      unsubscribeTfStarted()
      unsubscribeTfPoseAnalyzed()
      unsubscribeTfCompleted()
      unsubscribeTfFailed()
      unsubscribeVcStarted()
      unsubscribeVcCompleted()
      unsubscribeVcFailed()
      unsubscribeVcBatchDone()
      unsubscribeCcStarted()
      unsubscribeCcCompleted()
      unsubscribeCcFailed()
      unsubscribeCcBatchDone()
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
