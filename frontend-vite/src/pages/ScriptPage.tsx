// pages/ScriptPage.tsx - 脚本审批页

import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Loader2, RefreshCw, CheckCircle, Edit3 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Badge } from '@/components/ui/badge'
import { ShotCard } from '@/components/ShotCard'
import { ProgressStream } from '@/components/ProgressStream'
import { ReferenceAssetsPanel } from '@/components/ReferenceAssetsPanel'
import { api } from '@/lib/api'
import { useStore } from '@/lib/state'
import type { ProjectStatus, ReferenceImage, Shot } from '@/lib/types'

export default function ScriptPage() {
  const navigate = useNavigate()
  const { id: projectId } = useParams<{ id: string }>()
  const {
    addToast,
    currentProject,
    setCurrentProject,
    shots,
    setShots,
    updateShot,
  } = useStore()

  const [status, setStatus] = useState<ProjectStatus>('scripting')
  const [sceneOverview, setSceneOverview] = useState('')
  const [referenceImages, setReferenceImages] = useState<ReferenceImage[]>([])
  const [isSaving, setIsSaving] = useState(false)
  const [isApproving, setIsApproving] = useState(false)

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
        setReferenceImages(project.reference_images || [])
      } catch (error) {
        addToast({
          type: 'error',
          message: error instanceof Error ? error.message : '获取项目失败',
        })
      }
    }

    fetchProject()
  }, [projectId, setCurrentProject, setShots, addToast])

  // 处理 SSE 事件
  const handleSSEEvent = useCallback((type: string, data: unknown) => {
    if (type === 'script_ready') {
      const scriptData = data as { storyboard: { scene_overview?: string; shots: typeof shots } }
      if (scriptData.storyboard.scene_overview) {
        setSceneOverview(scriptData.storyboard.scene_overview)
      }
      setShots(scriptData.storyboard.shots)
      setStatus('script_review')
    }
  }, [setShots])

  // 更新 scene_overview
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

  // 编辑 shot 台词 + 视觉描述 + 连续/断开
  const handleEditScript = async (shotId: number, newText: string, newVisual: string, newAlign: boolean) => {
    if (!projectId) return
    try {
      await api.patchShot(projectId, shotId, { text: newText, visual_description: newVisual, align_with_previous: newAlign })
      updateShot(shotId, { text: newText, visual_description: newVisual, align_with_previous: newAlign })
      addToast({ type: 'success', message: '分镜已更新' })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '更新失败',
      })
    }
  }

  // 切换对齐状态
  const handleToggleAlign = async (shotId: number, align: boolean) => {
    if (!projectId) return
    try {
      await api.patchShot(projectId, shotId, { align_with_previous: align })
      updateShot(shotId, { align_with_previous: align })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '更新失败',
      })
    }
  }

  // 更新 shot 字段（参考图上传/删除后）
  const handleShotUpdated = useCallback((shotId: number, updates: Partial<Shot>) => {
    setShots(shots.map((s) => (s.shot_id === shotId ? { ...s, ...updates } : s)))
  }, [shots, setShots])

  // 重新生成脚本
  const handleRegenerate = async () => {
    if (!projectId) return
    if (!confirm('确定要重新生成脚本吗？当前脚本内容将被替换。')) return

    try {
      await api.regenerateScript(projectId)
      setStatus('scripting')
      addToast({ type: 'info', message: '开始重新生成脚本' })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '操作失败',
      })
    }
  }

  // 通过并开始生成分镜
  const handleApprove = async () => {
    if (!projectId) return
    setIsApproving(true)
    try {
      await api.approveScript(projectId)
      addToast({ type: 'success', message: '脚本已通过，开始生成分镜' })
      navigate(`/projects/${projectId}/shots`)
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '操作失败',
      })
      setIsApproving(false)
    }
  }

  // SCRIPTING 状态 - 显示加载和进度
  if (status === 'scripting') {
    return (
      <div className="min-h-screen bg-zinc-50">
        <header className="bg-white border-b">
          <div className="max-w-5xl mx-auto px-4 h-14 flex items-center gap-4">
            <Button variant="ghost" size="icon" onClick={() => navigate('/')}>
              <ArrowLeft className="w-5 h-5" />
            </Button>
            <h1 className="text-lg font-medium">{currentProject?.title || '加载中...'}</h1>
            <Badge variant="secondary" className="bg-blue-100 text-blue-700">生成脚本中</Badge>
          </div>
        </header>

        <main className="max-w-5xl mx-auto px-4 py-8">
          <div data-testid="script-loading" className="flex flex-col items-center justify-center py-16">
            <Loader2 className="w-12 h-12 animate-spin text-blue-600 mb-4" />
            <p className="text-zinc-500">正在生成脚本，请稍候...</p>
          </div>
          {projectId && <ProgressStream projectId={projectId} onEvent={handleSSEEvent} />}
        </main>
      </div>
    )
  }

  // SCRIPT_REVIEW 状态 - 显示脚本审批界面
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
            <Badge variant="secondary" className="bg-yellow-100 text-yellow-700">脚本审批</Badge>
          </div>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-4 py-6">
        {/* Scene Overview */}
        <div data-testid="script-content" className="bg-white rounded-lg shadow-sm p-6 mb-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-medium">场景概览</h2>
            <Button
              variant="outline"
              size="sm"
              onClick={handleSaveOverview}
              disabled={isSaving}
            >
              {isSaving ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <><Edit3 className="w-4 h-4 mr-1" />保存</>
              )}
            </Button>
          </div>
          <Textarea
            value={sceneOverview}
            onChange={(e) => setSceneOverview(e.target.value)}
            rows={3}
            placeholder="描述整体场景氛围..."
          />
        </div>

        {/* Reference assets (images only on the script-review page) */}
        {referenceImages.length > 0 && (
          <div className="mb-6">
            <ReferenceAssetsPanel images={referenceImages} />
          </div>
        )}

        {/* Shots List */}
        <div className="space-y-4">
          <h2 className="text-lg font-medium">分镜列表 ({shots.length})</h2>
          {shots.map((shot) => (
            <ShotCard
              key={shot.id}
              shot={shot}
              variant="script"
              projectId={projectId}
              onEditScript={handleEditScript}
              onToggleAlign={handleToggleAlign}
              onShotUpdated={handleShotUpdated}
            />
          ))}
        </div>

        {/* Actions */}
        <div className="flex justify-between items-center mt-8 pt-6 border-t sticky bottom-0 bg-zinc-50 py-4">
          <Button data-testid="regenerate-script-button" variant="outline" onClick={handleRegenerate}>
            <RefreshCw className="w-4 h-4 mr-2" />
            重新生成脚本
          </Button>
          <Button data-testid="approve-script-button" onClick={handleApprove} disabled={isApproving}>
            {isApproving ? (
              <>
                <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                处理中...
              </>
            ) : (
              <>
                <CheckCircle className="w-4 h-4 mr-2" />
                通过，开始生成视频
              </>
            )}
          </Button>
        </div>
      </main>
    </div>
  )
}
