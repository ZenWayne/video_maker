// pages/ShotsPage.tsx - 统一分镜编辑审批页

import { useCallback, useEffect, useState, useMemo } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  ArrowLeft,
  ArrowLeftCircle,
  RefreshCw,
  CheckCircle,
  AlertTriangle,
  Plus,
  Edit3,
  Loader2,
  XCircle,
  Mic,
  User,
  Film,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Card } from '@/components/ui/card'
import { Textarea } from '@/components/ui/textarea'
import { ShotCard } from '@/components/ShotCard'
import { ProgressStream } from '@/components/ProgressStream'
import { VoiceCalibrationPanel } from '@/components/VoiceCalibrationPanel'
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import { api } from '@/lib/api'
import { useStore } from '@/lib/state'
import type { ProjectStatus, Shot } from '@/lib/types'

// 计算断层警告
function computeCascadeWarnings(
  shots: Shot[],
  selectedIds: Set<number>
): Map<number, number[]> {
  const warnings = new Map<number, number[]>()

  for (const id of selectedIds) {
    const downstream: number[] = []
    let cursor = id + 1
    while (cursor <= shots.length) {
      const s = shots.find((s) => s.shot_id === cursor)
      if (!s || !s.align_with_previous) break
      if (!selectedIds.has(cursor)) {
        downstream.push(cursor)
      }
      cursor++
    }
    if (downstream.length > 0) {
      warnings.set(id, downstream)
    }
  }

  return warnings
}

export default function ShotsPage() {
  const navigate = useNavigate()
  const { id: projectId } = useParams<{ id: string }>()
  const {
    addToast,
    currentProject,
    setCurrentProject,
    shots,
    setShots,
    selectedShotIds,
    toggleShotSelection,
    clearSelection,
  } = useStore()

  const [status, setStatus] = useState<ProjectStatus>('shot_generating')
  const [sceneOverview, setSceneOverview] = useState('')
  const [isSaving, setIsSaving] = useState(false)
  const [isExporting, setIsExporting] = useState(false)
  const [isRegenerating, setIsRegenerating] = useState(false)
  const [isContinuing, setIsContinuing] = useState(false)
  const [isApproving, setIsApproving] = useState(false)
  const [isRegeneratingScript, setIsRegeneratingScript] = useState(false)
  const [isCancelling, setIsCancelling] = useState(false)
  const [referenceVoiceShotId, setReferenceVoiceShotId] = useState<number | null>(null)
  const [referenceVoicePath, setReferenceVoicePath] = useState<string | null>(null)
  const [autoVoiceCalibrate, setAutoVoiceCalibrate] = useState(false)
  const [isVcConverting, setIsVcConverting] = useState(false)
  const [isCcCalibrating, setIsCcCalibrating] = useState(false)
  const [hasCharacterRefs, setHasCharacterRefs] = useState(false)
  const [joinPreviewUrl, setJoinPreviewUrl] = useState<string | null>(null)
  const [isJoining, setIsJoining] = useState(false)

  const updateShot = useStore((s) => s.updateShot)

  const handleSSEEvent = useCallback((type: string, data?: unknown) => {
    if (type === 'all_shots_ready' || type === 'shot_review_ready') {
      setStatus('shot_review')
    }
    if (type === 'script_ready') {
      const scriptData = data as { storyboard: { scene_overview?: string; shots: Shot[] } }
      if (scriptData.storyboard.scene_overview) {
        setSceneOverview(scriptData.storyboard.scene_overview)
      }
      setShots(scriptData.storyboard.shots)
      setStatus('script_review')
    }
    // VC/CC batch completion — reset loading states
    // (individual shot updates are handled by ProgressStream directly)
    if (type === 'vc_batch_done') {
      setIsVcConverting(false)
    }
    if (type === 'cc_batch_done') {
      setIsCcCalibrating(false)
    }
    // Tail frame completion — ensure page returns to shot_review
    if (type === 'tf_completed') {
      setStatus('shot_review')
    }
  }, [setShots, updateShot])

  // 获取项目详情
  useEffect(() => {
    if (!projectId) return

    const fetchProject = async () => {
      try {
        const project = await api.getProject(projectId)
        setCurrentProject(project)
        setStatus(project.status as ProjectStatus)
        setSceneOverview(project.scene_overview || '')
        setShots(project.shots || [])
        setReferenceVoiceShotId(project.reference_voice_shot_id ?? null)
        setReferenceVoicePath(project.reference_voice_path ?? null)
        setAutoVoiceCalibrate(project.auto_voice_calibrate ?? false)
        setHasCharacterRefs(project.reference_images?.some((r) => r.kind === 'character') ?? false)
      } catch (error) {
        addToast({
          type: 'error',
          message: error instanceof Error ? error.message : '获取项目失败',
        })
      }
    }

    fetchProject()
  }, [projectId, setCurrentProject, setShots, addToast])

  // 计算断层警告
  const warnings = useMemo(() => {
    return computeCascadeWarnings(shots, selectedShotIds)
  }, [shots, selectedShotIds])

  // 检查是否有失败的 shot
  const hasFailedShots = shots.some((s) => s.status === 'failed')
  const hasPendingShots = shots.some((s) => s.status === 'pending' || s.status === 'failed')
  const completedCount = shots.filter((s) => s.status === 'completed').length

  // 全选/取消全选
  const handleSelectAll = () => {
    if (selectedShotIds.size === shots.length) {
      clearSelection()
    } else {
      shots.forEach((s) => {
        if (!selectedShotIds.has(s.shot_id)) {
          toggleShotSelection(s.shot_id)
        }
      })
    }
  }

  // 追加下游镜头到选中
  const handleAppendDownstream = (shotId: number) => {
    const downstream = warnings.get(shotId) || []
    downstream.forEach((id) => {
      if (!selectedShotIds.has(id)) {
        toggleShotSelection(id)
      }
    })
  }

  // 连贯性预览：临时拼接选中镜头
  const handleJoinPreview = async () => {
    if (!projectId || selectedShotIds.size < 2) return
    setIsJoining(true)
    try {
      const ids = Array.from(selectedShotIds).sort((a, b) => a - b)
      const { preview_url } = await api.joinPreview(projectId, ids)
      setJoinPreviewUrl(preview_url)
    } catch (e) {
      addToast({
        type: 'error',
        message: e instanceof Error ? e.message : '拼接预览失败',
      })
    } finally {
      setIsJoining(false)
    }
  }

  // 重新生成选中的 shots
  const handleRegenerate = async () => {
    if (!projectId) return
    if (selectedShotIds.size === 0) {
      addToast({ type: 'warning', message: '请先选择要重跑的分镜' })
      return
    }

    setIsRegenerating(true)
    try {
      const ids = Array.from(selectedShotIds)
      await api.regenerateShots(projectId, ids)
      // Clear stale video so the old clip doesn't keep showing
      setShots(shots.map((s) =>
        ids.includes(s.shot_id)
          ? { ...s, status: 'pending', video_path: undefined, last_frame_path: undefined }
          : s
      ))
      clearSelection()
      setStatus('shot_generating')
      addToast({ type: 'success', message: '开始重新生成选中的分镜' })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '操作失败',
      })
    } finally {
      setIsRegenerating(false)
    }
  }

  // 更新 motion_prompt
  const handleEditPrompt = async (shotId: number, prompt: string) => {
    if (!projectId) return
    try {
      await api.patchShot(projectId, shotId, { motion_prompt: prompt })
      addToast({ type: 'success', message: '运镜提示词已更新' })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '更新失败',
      })
    }
  }

  // 导出视频
  const handleExport = async () => {
    if (!projectId) return
    if (hasFailedShots) {
      addToast({ type: 'error', message: '存在失败的分镜，无法导出' })
      return
    }

    setIsExporting(true)
    try {
      await api.exportVideo(projectId)
      navigate(`/projects/${projectId}/export`)
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '导出失败',
      })
      setIsExporting(false)
    }
  }

  // 退回脚本审批
  const handleBackToScript = async () => {
    if (!projectId) return
    try {
      await api.resetToScript(projectId)
      setStatus('script_review')
      addToast({ type: 'success', message: '已退回脚本审批' })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '操作失败',
      })
    }
  }

  // 通过脚本，开始生成视频
  const handleApproveScript = async () => {
    if (!projectId) return
    setIsApproving(true)
    try {
      await api.approveScript(projectId)
      setStatus('shot_generating')
      addToast({ type: 'success', message: '开始生成尾帧' })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '操作失败',
      })
    } finally {
      setIsApproving(false)
    }
  }

  // 重新生成脚本
  const handleRegenerateScript = async () => {
    if (!projectId) return
    if (!confirm('确定要重新生成脚本吗？当前脚本将被替换。')) return
    setIsRegeneratingScript(true)
    try {
      await api.regenerateScript(projectId)
      setStatus('scripting')
      addToast({ type: 'success', message: '开始重新生成脚本' })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '操作失败',
      })
    } finally {
      setIsRegeneratingScript(false)
    }
  }

  // 通过当前镜头，继续生成下一个
  const handleContinueGeneration = async () => {
    if (!projectId) return
    setIsContinuing(true)
    try {
      await api.continueGeneration(projectId)
      setStatus('shot_generating')
      addToast({ type: 'success', message: '开始生成下一个镜头' })
    } catch (error) {
      const msg = error instanceof Error ? error.message : '操作失败'
      const isValidation = msg.includes('尾帧')
      addToast({
        type: isValidation ? 'warning' : 'error',
        message: msg,
      })
    } finally {
      setIsContinuing(false)
    }
  }

  // 取消生成
  const handleCancelGeneration = async () => {
    if (!projectId) return
    setIsCancelling(true)
    try {
      await api.cancelGeneration(projectId)
      setStatus('shot_review')
      addToast({ type: 'success', message: '已取消生成' })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '取消失败',
      })
    } finally {
      setIsCancelling(false)
    }
  }

  // 重新生成单个镜头
  const handleRedrawShot = async (shotId: number) => {
    if (!projectId) return
    try {
      await api.regenerateShots(projectId, [shotId])
      // Clear stale video so the old clip doesn't keep showing
      setShots(shots.map((s) =>
        s.shot_id === shotId
          ? { ...s, status: 'pending', video_path: undefined, last_frame_path: undefined }
          : s
      ))
      setStatus('shot_generating')
      addToast({ type: 'success', message: `开始重新生成镜头 #${shotId}` })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '操作失败',
      })
    }
  }

  // 更新 shot 字段
  const handleShotUpdated = useCallback((shotId: number, updates: Partial<Shot>) => {
    setShots(shots.map((s) => (s.shot_id === shotId ? { ...s, ...updates } : s)))
  }, [shots, setShots])

  // 设置/取消基准音色
  const handleSetReferenceVoice = async (shotId: number) => {
    if (!projectId) return
    try {
      if (referenceVoiceShotId === shotId) {
        await api.clearReferenceVoice(projectId)
        setReferenceVoiceShotId(null)
        addToast({ type: 'success', message: '已清除基准音色' })
      } else {
        await api.setReferenceVoice(projectId, shotId)
        setReferenceVoiceShotId(shotId)
        addToast({ type: 'success', message: `已设置镜头 #${shotId} 为基准音色` })
      }
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '操作失败',
      })
    }
  }

  // 单个 shot 音色转换
  const handleVoiceConvert = async (shotId: number) => {
    if (!projectId) return
    try {
      await api.voiceConvert(projectId, shotId)
      updateShot(shotId, { vc_status: 'converting' })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '转换失败',
      })
    }
  }

  // 还原音色
  const handleVoiceRevert = async (shotId: number) => {
    if (!projectId) return
    try {
      const result = await api.voiceRevert(projectId, shotId)
      updateShot(shotId, { vc_status: null, vc_error_message: null, video_path: `${result.video_path}?v=${result.version}` })
      addToast({ type: 'success', message: '已还原原始音色' })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '还原失败',
      })
    }
  }

  // 上传基准音色文件
  const handleUploadReferenceVoice = async (file: File) => {
    if (!projectId) return
    try {
      const res = await api.uploadReferenceVoice(projectId, file)
      setReferenceVoiceShotId(null)
      setReferenceVoicePath(res.reference_voice_path)
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '上传音色文件失败',
      })
    }
  }

  // 移除基准音色
  const handleRemoveReferenceVoice = async () => {
    if (!projectId) return
    try {
      await api.clearReferenceVoice(projectId)
      setReferenceVoiceShotId(null)
      setReferenceVoicePath(null)
      setAutoVoiceCalibrate(false)
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '移除音色失败',
      })
    }
  }

  // 切换自动音色校准
  const handleToggleAutoCalibrate = async (enabled: boolean) => {
    if (!projectId) return
    try {
      const res = await api.setAutoVoiceCalibrate(projectId, enabled)
      setAutoVoiceCalibrate(res.auto_voice_calibrate)
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '设置自动音色校准失败',
      })
    }
  }

  // 一键统一音色
  const handleVoiceConvertAll = async () => {
    if (!projectId) return
    setIsVcConverting(true)
    try {
      await api.voiceConvertAll(projectId)
      addToast({ type: 'success', message: '开始批量转换音色' })
    } catch (error) {
      setIsVcConverting(false)
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '操作失败',
      })
    }
  }

  // 单个 shot 人物校准
  const handleCharacterCalibrate = async (shotId: number) => {
    if (!projectId) return
    try {
      await api.characterCalibrate(projectId, shotId)
      updateShot(shotId, { cc_status: 'calibrating' })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '校准失败',
      })
    }
  }

  // 还原人物校准
  const handleCharacterCalibrateRevert = async (shotId: number) => {
    if (!projectId) return
    try {
      const result = await api.characterCalibrateRevert(projectId, shotId)
      updateShot(shotId, { cc_status: null, cc_error_message: null, last_frame_path: `${result.last_frame_path}?v=${result.version}` })
      addToast({ type: 'success', message: '已还原末帧' })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '还原失败',
      })
    }
  }

  // 一键人物校准
  const handleCharacterCalibrateAll = async () => {
    if (!projectId) return
    setIsCcCalibrating(true)
    try {
      await api.characterCalibrateAll(projectId)
      addToast({ type: 'success', message: '开始批量人物校准' })
    } catch (error) {
      setIsCcCalibrating(false)
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '操作失败',
      })
    }
  }

  // 生成尾帧
  const handleGenerateTailFrame = async (shotId: number) => {
    if (!projectId) return
    try {
      await api.generateTailFrame(projectId, shotId)
      updateShot(shotId, { tf_status: 'generating', tf_confirmed: false })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '尾帧生成失败',
      })
    }
  }

  // 确认尾帧并生成视频
  const handleConfirmTailFrame = async (shotId: number) => {
    if (!projectId) return
    try {
      await api.confirmTailFrame(projectId, shotId)
      updateShot(shotId, { tf_confirmed: true })
      setStatus('shot_generating')
      addToast({ type: 'success', message: `镜头 #${shotId} 尾帧已确认，开始生成视频` })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '确认失败',
      })
    }
  }

  // 从视频提取尾帧
  const handleExtractTailFrame = async (shotId: number) => {
    if (!projectId) return
    try {
      const result = await api.extractTailFrame(projectId, shotId)
      updateShot(shotId, {
        target_last_frame_path: `${result.target_last_frame_path}?t=${Date.now()}`,
        tf_status: 'done' as const,
        tf_confirmed: false,
      })
      addToast({ type: 'success', message: `镜头 #${shotId} 视频尾帧已提取为目标尾帧` })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '提取失败',
      })
    }
  }

  // 删除尾帧（清空尾帧状态，不自动生成视频）
  const handleDeleteTailFrame = async (shotId: number) => {
    if (!projectId) return
    if (!confirm('确定删除该镜头的目标尾帧？删除后需重新生成。')) return
    try {
      await api.deleteTailFrame(projectId, shotId)
      updateShot(shotId, {
        tf_status: null,
        tf_confirmed: false,
        target_last_frame_path: null,
        skip_tail_frame: true,
      })
      addToast({ type: 'success', message: `镜头 #${shotId} 尾帧已删除` })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '删除失败',
      })
    }
  }

  // 保存 scene_overview
  const handleSaveOverview = async () => {
    if (!projectId) return
    setIsSaving(true)
    try {
      await api.patchStoryboard(projectId, { scene_overview: sceneOverview })
      addToast({ type: 'success', message: '场景概览已更新' })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '保存失败',
      })
    } finally {
      setIsSaving(false)
    }
  }

  // Status badge
  const statusBadge = (() => {
    switch (status) {
      case 'scripting': return <Badge variant="secondary" className="bg-blue-100 text-blue-700">生成脚本中</Badge>
      case 'script_review': return <Badge variant="secondary" className="bg-yellow-100 text-yellow-700">脚本审批</Badge>
      case 'shot_generating': return <Badge variant="secondary" className="bg-blue-100 text-blue-700">生成分镜中</Badge>
      case 'shot_review': return <Badge variant="secondary" className="bg-yellow-100 text-yellow-700">分镜审批</Badge>
      default: return null
    }
  })()

  // SHOT_GENERATING 状态
  if (status === 'shot_generating') {
    return (
      <div className="min-h-screen bg-zinc-50">
        <header className="bg-white border-b">
          <div className="max-w-5xl mx-auto px-4 h-14 flex items-center gap-4">
            <Button variant="ghost" size="icon" onClick={() => navigate('/')}>
              <ArrowLeft className="w-5 h-5" />
            </Button>
            <h1 className="text-lg font-medium">{currentProject?.title}</h1>
            {statusBadge}
            <Button
              variant="outline"
              size="sm"
              onClick={handleCancelGeneration}
              disabled={isCancelling}
            >
              {isCancelling ? (
                <Loader2 className="w-4 h-4 mr-1 animate-spin" />
              ) : (
                <XCircle className="w-4 h-4 mr-1" />
              )}
              取消生成
            </Button>
          </div>
        </header>

        <main className="max-w-5xl mx-auto px-4 py-8">
          {projectId && (
            <ProgressStream
              projectId={projectId}
              onEvent={handleSSEEvent}
            />
          )}

          <div data-testid="shots-list" className="mt-8 grid grid-cols-1 md:grid-cols-2 gap-4">
            {shots.map((shot, idx) => {
              const isActive = shot.status === 'prompt_generating' || shot.status === 'video_generating'
              const prevShot = idx > 0 ? shots[idx - 1] : null
              return isActive ? (
                <ShotCard key={shot.id} shot={shot} variant="generating" />
              ) : (
                <ShotCard
                  key={shot.id}
                  shot={shot}
                  variant="review"
                  projectId={projectId!}
                  aspectRatio={currentProject?.aspect_ratio}
                  prevLastFramePath={prevShot?.last_frame_path}
                  isReferenceVoice={referenceVoiceShotId === shot.shot_id}
                  hasReferenceVoice={referenceVoiceShotId != null}
                  onEditPrompt={handleEditPrompt}
                  onRedraw={handleRedrawShot}
                  onShotUpdated={handleShotUpdated}
                  onSetReferenceVoice={handleSetReferenceVoice}
                  onVoiceConvert={handleVoiceConvert}
                  onVoiceRevert={handleVoiceRevert}
                  onCharacterCalibrate={handleCharacterCalibrate}
                  onCharacterCalibrateRevert={handleCharacterCalibrateRevert}
                  onGenerateTailFrame={handleGenerateTailFrame}
                  onConfirmTailFrame={handleConfirmTailFrame}
                  onExtractTailFrame={handleExtractTailFrame}
                  onDeleteTailFrame={handleDeleteTailFrame}
                />
              )
            })}
          </div>
        </main>
      </div>
    )
  }

  // SCRIPTING 状态（等待脚本生成）
  if (status === 'scripting') {
    return (
      <div className="min-h-screen bg-zinc-50">
        <header className="bg-white border-b">
          <div className="max-w-5xl mx-auto px-4 h-14 flex items-center gap-4">
            <Button variant="ghost" size="icon" onClick={() => navigate('/')}>
              <ArrowLeft className="w-5 h-5" />
            </Button>
            <h1 className="text-lg font-medium">{currentProject?.title}</h1>
            {statusBadge}
          </div>
        </header>
        <main className="max-w-5xl mx-auto px-4 py-8">
          {projectId && (
            <ProgressStream
              projectId={projectId}
              onEvent={handleSSEEvent}
            />
          )}
          <div className="mt-8 text-center text-zinc-500">
            <Loader2 className="w-8 h-8 animate-spin mx-auto mb-4" />
            正在生成脚本...
          </div>
        </main>
      </div>
    )
  }

  // SCRIPT_REVIEW / SHOT_REVIEW 状态（统一编辑布局）
  return (
    <div className="min-h-screen bg-zinc-50">
      {/* Header */}
      <header className="bg-white border-b sticky top-0 z-10">
        <div className="max-w-5xl mx-auto px-4 h-14 flex items-center justify-between">
          <div className="flex items-center gap-4">
            <Button variant="ghost" size="icon" onClick={() => navigate('/')}>
              <ArrowLeft className="w-5 h-5" />
            </Button>
            <h1 className="text-lg font-medium">{currentProject?.title}</h1>
            {statusBadge}
            {shots.length > 0 && (
              <span className="text-sm text-zinc-500">
                镜头 {completedCount}/{shots.length}
              </span>
            )}
          </div>

          {status !== 'script_review' && (
            <div className="flex items-center gap-2">
              <span className="text-sm text-zinc-500">已选 {selectedShotIds.size} 个</span>
              <Button variant="outline" size="sm" onClick={handleSelectAll}>
                {selectedShotIds.size === shots.length ? '取消全选' : '全选'}
              </Button>
            </div>
          )}
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-4 py-6">
        {/* SSE subscription for tail frame and other async events */}
        {projectId && status === 'shot_review' && (
          <div className={shots.some(s => s.tf_status === 'generating' || s.vc_status === 'converting' || s.cc_status === 'calibrating') ? '' : 'hidden'}>
            <ProgressStream projectId={projectId} onEvent={handleSSEEvent} />
          </div>
        )}

        {/* Scene Overview */}
        {sceneOverview !== undefined && (
          <div data-testid="script-content" className="mb-6">
            <div className="flex items-center justify-between mb-2">
              <h2 className="text-sm font-medium text-zinc-700">场景概览</h2>
              <Button
                variant="ghost"
                size="sm"
                onClick={handleSaveOverview}
                disabled={isSaving}
              >
                {isSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Edit3 className="w-3 h-3 mr-1" />保存</>}
              </Button>
            </div>
            <Textarea
              value={sceneOverview}
              onChange={(e) => setSceneOverview(e.target.value)}
              rows={2}
              placeholder="描述整体场景氛围..."
              className="text-sm"
            />
          </div>
        )}

        {/* Warnings */}
        {warnings.size > 0 && (
          <Card className="mb-6 p-4 bg-yellow-50 border-yellow-200">
            <div className="flex items-start gap-3">
              <AlertTriangle className="w-5 h-5 text-yellow-600 mt-0.5" />
              <div className="flex-1">
                <h3 className="font-medium text-yellow-800 mb-2">断层警告</h3>
                <div className="space-y-2">
                  {Array.from(warnings.entries()).map(([shotId, downstream]) => (
                    <div key={shotId} className="text-sm text-yellow-700">
                      <span>分镜 #{shotId} 的下游 [{downstream.join(', ')}] 是连续镜头，
                      只重跑 #{shotId} 可能导致衔接断层。</span>
                      <Button
                        variant="link"
                        size="sm"
                        className="text-yellow-800 h-auto p-0 ml-2"
                        onClick={() => handleAppendDownstream(shotId)}
                      >
                        <Plus className="w-3 h-3 mr-1" />一键追加
                      </Button>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </Card>
        )}

        {/* Shots Grid */}
        <div data-testid="shots-list" className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {shots.map((shot, idx) => {
            const prevShot = idx > 0 ? shots[idx - 1] : null
            return (
              <ShotCard
                key={shot.id || shot.shot_id}
                shot={shot}
                variant="review"
                projectId={projectId}
                aspectRatio={currentProject?.aspect_ratio}
                selected={selectedShotIds.has(shot.shot_id)}
                prevLastFramePath={prevShot?.last_frame_path}
                isReferenceVoice={referenceVoiceShotId === shot.shot_id}
                hasReferenceVoice={referenceVoiceShotId != null}
                autoVoiceCalibrate={autoVoiceCalibrate}
                onSelect={status !== 'script_review' ? toggleShotSelection : undefined}
                onEditPrompt={handleEditPrompt}
                onRedraw={handleRedrawShot}
                onShotUpdated={handleShotUpdated}
                onSetReferenceVoice={status !== 'script_review' ? handleSetReferenceVoice : undefined}
                onVoiceConvert={status !== 'script_review' ? handleVoiceConvert : undefined}
                onVoiceRevert={status !== 'script_review' ? handleVoiceRevert : undefined}
                onCharacterCalibrate={status !== 'script_review' ? handleCharacterCalibrate : undefined}
                onCharacterCalibrateRevert={status !== 'script_review' ? handleCharacterCalibrateRevert : undefined}
                onGenerateTailFrame={status !== 'script_review' ? handleGenerateTailFrame : undefined}
                onConfirmTailFrame={status !== 'script_review' ? handleConfirmTailFrame : undefined}
                onExtractTailFrame={status !== 'script_review' ? handleExtractTailFrame : undefined}
                onDeleteTailFrame={status !== 'script_review' ? handleDeleteTailFrame : undefined}
              />
            )
          })}
        </div>

        {/* Voice Calibration Panel */}
        {status === 'shot_review' && (
          <VoiceCalibrationPanel
            referenceVoicePath={referenceVoicePath}
            referenceVoiceShotId={referenceVoiceShotId}
            autoVoiceCalibrate={autoVoiceCalibrate}
            onUpload={handleUploadReferenceVoice}
            onRemove={handleRemoveReferenceVoice}
            onToggleAuto={handleToggleAutoCalibrate}
            onCalibrateAll={handleVoiceConvertAll}
          />
        )}

        {/* Actions */}
        <div className="flex flex-wrap items-center justify-between gap-4 mt-8 pt-6 border-t sticky bottom-0 bg-zinc-50 py-4">
          {status === 'script_review' ? (
            <>
              <Button
                variant="outline"
                onClick={handleRegenerateScript}
                disabled={isRegeneratingScript}
              >
                {isRegeneratingScript ? (
                  <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                ) : (
                  <RefreshCw className="w-4 h-4 mr-2" />
                )}
                重新生成脚本
              </Button>
              <Button
                data-testid="approve-script-button"
                onClick={handleApproveScript}
                disabled={isApproving || shots.length === 0}
              >
                {isApproving ? (
                  <>
                    <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                    处理中...
                  </>
                ) : (
                  <>
                    <CheckCircle className="w-4 h-4 mr-2" />
                    通过，开始生成尾帧
                  </>
                )}
              </Button>
            </>
          ) : (
            <>
              <Button variant="outline" onClick={handleBackToScript}>
                <ArrowLeftCircle className="w-4 h-4 mr-2" />
                退回修改脚本
              </Button>

              <div className="flex gap-3">
                <Button
                  variant="outline"
                  data-testid="join-preview-button"
                  onClick={handleJoinPreview}
                  disabled={selectedShotIds.size < 2 || isJoining}
                >
                  {isJoining ? (
                    <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                  ) : (
                    <Film className="w-4 h-4 mr-2" />
                  )}
                  连贯性预览
                </Button>

                <Button
                  variant="outline"
                  onClick={handleRegenerate}
                  disabled={selectedShotIds.size === 0 || isRegenerating}
                >
                  {isRegenerating ? (
                    <RefreshCw className="w-4 h-4 mr-2 animate-spin" />
                  ) : (
                    <RefreshCw className="w-4 h-4 mr-2" />
                  )}
                  重跑选中的镜
                </Button>

                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant="outline"
                      onClick={handleVoiceConvertAll}
                      disabled={!referenceVoiceShotId || isVcConverting}
                    >
                      {isVcConverting ? (
                        <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                      ) : (
                        <Mic className="w-4 h-4 mr-2" />
                      )}
                      {referenceVoiceShotId
                        ? `统一音色 (基准: #${referenceVoiceShotId})`
                        : '统一音色'}
                    </Button>
                  </TooltipTrigger>
                  {!referenceVoiceShotId && (
                    <TooltipContent>
                      <p>请先在镜头卡片上设置基准音色</p>
                    </TooltipContent>
                  )}
                </Tooltip>

                <Button
                  variant="outline"
                  onClick={handleCharacterCalibrateAll}
                  disabled={isCcCalibrating || !hasCharacterRefs}
                >
                  {isCcCalibrating ? (
                    <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                  ) : (
                    <User className="w-4 h-4 mr-2" />
                  )}
                  全部人物校准
                </Button>

                {hasPendingShots ? (
                  <Button
                    onClick={handleContinueGeneration}
                    disabled={isContinuing || completedCount === 0}
                  >
                    {isContinuing ? (
                      <>处理中...</>
                    ) : (
                      <>
                        <CheckCircle className="w-4 h-4 mr-2" />
                        继续下一个（{completedCount}/{shots.length}）
                      </>
                    )}
                  </Button>
                ) : (
                  <Tooltip>
                    <TooltipTrigger
                      className="disabled:opacity-50 disabled:cursor-not-allowed"
                      disabled={hasFailedShots || isExporting}
                      onClick={handleExport}
                    >
                      <Button
                        data-testid="export-button"
                        disabled={hasFailedShots || isExporting}
                      >
                        {isExporting ? (
                          <>处理中...</>
                        ) : (
                          <>
                            <CheckCircle className="w-4 h-4 mr-2" />
                            全部通过，导出
                          </>
                        )}
                      </Button>
                    </TooltipTrigger>
                    {hasFailedShots && (
                      <TooltipContent>
                        <p>存在失败的分镜，请先重跑或修复</p>
                      </TooltipContent>
                    )}
                  </Tooltip>
                )}
              </div>
            </>
          )}
        </div>
      </main>

      {joinPreviewUrl && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70"
          onClick={() => setJoinPreviewUrl(null)}
          data-testid="join-preview-modal"
        >
          <div
            className="relative bg-zinc-900 rounded-lg p-4 max-w-3xl w-full mx-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between mb-3">
              <span className="text-sm text-zinc-300">连贯性预览（临时拼接）</span>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setJoinPreviewUrl(null)}
              >
                关闭
              </Button>
            </div>
            <video
              src={joinPreviewUrl}
              controls
              autoPlay
              className="w-full rounded"
            />
          </div>
        </div>
      )}
    </div>
  )
}
