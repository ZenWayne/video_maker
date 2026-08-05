// pages/NewProjectPage.tsx - 新建项目页

import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, Link2, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { UploadZone } from '@/components/UploadZone'
import { api } from '@/lib/api'
import { useStore } from '@/lib/state'
import type { ContentAnalysis } from '@/lib/types'

export default function NewProjectPage() {
  const navigate = useNavigate()
  const { addToast } = useStore()
  const [isSubmitting, setIsSubmitting] = useState(false)

  const [title, setTitle] = useState('')
  const [themeText, setThemeText] = useState('')
  const [aspectRatio, setAspectRatio] = useState<'16:9' | '9:16'>('9:16')
  const [characterImages, setCharacterImages] = useState<File[]>([])
  const [sceneImages, setSceneImages] = useState<File[]>([])
  const [completedAnalyses, setCompletedAnalyses] = useState<ContentAnalysis[]>([])
  const [selectedAnalysisId, setSelectedAnalysisId] = useState('')

  useEffect(() => {
    api
      .listAnalyses()
      .then((list) => setCompletedAnalyses(list.filter((a) => a.status === 'completed')))
      .catch(() => {
        // 简报列表拉取失败不影响新建项目主流程，静默忽略
      })
  }, [])

  const handleSubmit = async () => {
    if (!title.trim()) {
      addToast({ type: 'error', message: '请输入项目标题' })
      return
    }
    if (!themeText.trim()) {
      addToast({ type: 'error', message: '请输入主题描述' })
      return
    }
    if (characterImages.length === 0) {
      addToast({ type: 'error', message: '请上传至少一张角色参考图' })
      return
    }

    setIsSubmitting(true)
    try {
      // Step 1: 创建项目
      const { project_id } = await api.createProject({
        title: title.trim(),
        theme_text: themeText.trim(),
        aspect_ratio: aspectRatio,
      })

      // Step 2: 上传角色参考图
      await api.uploadReferenceImages(project_id, characterImages, 'character')

      // Step 3: 上传场景参考图（如果有）
      if (sceneImages.length > 0) {
        await api.uploadReferenceImages(project_id, sceneImages, 'scene')
      }

      // Step 4: 挂载爆款简报（可选，失败不阻断主流程；必须在 startPipeline 之前完成，
      // 否则 run_screenwriter worker 可能在 attached_brief_json 写入前读取项目行，导致简报被静默忽略）
      if (selectedAnalysisId) {
        try {
          await api.attachBrief(project_id, selectedAnalysisId)
        } catch (error) {
          addToast({
            type: 'error',
            message: error instanceof Error ? `简报挂载失败：${error.message}` : '简报挂载失败',
          })
        }
      }

      // Step 5: 启动 pipeline
      await api.startPipeline(project_id)

      addToast({ type: 'success', message: '项目创建成功，开始生成脚本' })
      navigate(`/projects/${project_id}/shots`)
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '创建项目失败',
      })
      setIsSubmitting(false)
    }
  }

  return (
    <div className="min-h-screen bg-zinc-50">
      {/* Header */}
      <header className="bg-white border-b">
        <div className="max-w-3xl mx-auto px-4 h-14 flex items-center gap-4">
          <Button variant="ghost" size="icon" onClick={() => navigate(-1)}>
            <ArrowLeft className="w-5 h-5" />
          </Button>
          <h1 className="text-lg font-medium">新建项目</h1>
        </div>
      </header>

      {/* Form */}
      <main className="max-w-3xl mx-auto px-4 py-8">
        <div className="bg-white rounded-lg shadow-sm p-6 space-y-6">
          {/* 项目标题 */}
          <div className="space-y-2">
            <Label htmlFor="title">
              项目标题 <span className="text-red-500">*</span>
            </Label>
            <Input
              data-testid="project-title-input"
              id="title"
              placeholder="给你的项目起个名字"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </div>

          {/* 主题描述 */}
          <div className="space-y-2">
            <Label htmlFor="theme">
              主题描述 <span className="text-red-500">*</span>
            </Label>
            <Textarea
              data-testid="project-theme-input"
              id="theme"
              placeholder="用一句话描述视频的主题内容..."
              value={themeText}
              onChange={(e) => setThemeText(e.target.value)}
              rows={3}
            />
          </div>

          {/* 画面比例 */}
          <div className="space-y-2">
            <Label>画面比例</Label>
            <div className="flex gap-3">
              <button
                type="button"
                data-testid="aspect-ratio-16-9"
                onClick={() => setAspectRatio('16:9')}
                className={`px-4 py-2 rounded-lg border text-sm font-medium transition-colors ${
                  aspectRatio === '16:9'
                    ? 'border-blue-500 bg-blue-50 text-blue-700'
                    : 'border-zinc-200 text-zinc-600 hover:border-zinc-300'
                }`}
              >
                横屏 16:9
              </button>
              <button
                type="button"
                data-testid="aspect-ratio-9-16"
                onClick={() => setAspectRatio('9:16')}
                className={`px-4 py-2 rounded-lg border text-sm font-medium transition-colors ${
                  aspectRatio === '9:16'
                    ? 'border-blue-500 bg-blue-50 text-blue-700'
                    : 'border-zinc-200 text-zinc-600 hover:border-zinc-300'
                }`}
              >
                竖屏 9:16
              </button>
            </div>
          </div>

          {/* 角色参考图 */}
          <UploadZone
            kind="character"
            maxFiles={3}
            value={characterImages}
            onChange={setCharacterImages}
          />

          {/* 场景参考图 */}
          <UploadZone
            kind="scene"
            maxFiles={3}
            value={sceneImages}
            onChange={setSceneImages}
          />

          {/* 挂载爆款简报 */}
          <div className="space-y-2">
            <Label htmlFor="attach-brief" className="flex items-center gap-1.5">
              <Link2 className="w-4 h-4 text-zinc-400" />
              挂载爆款简报（可选）
            </Label>
            <select
              id="attach-brief"
              data-testid="attach-brief-select"
              value={selectedAnalysisId}
              onChange={(e) => setSelectedAnalysisId(e.target.value)}
              className="w-full rounded-lg border border-zinc-200 px-3 py-2 text-sm text-zinc-700 focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="">无（不挂载）</option>
              {completedAnalyses.map((analysis) => (
                <option key={analysis.id} value={analysis.id}>
                  {analysis.title} · 🟢 已完成
                </option>
              ))}
            </select>
            <p className="text-xs text-zinc-400">
              选中后简报以快照写入项目，日后简报改动不回溯污染已建项目
            </p>
          </div>

          {/* 提交按钮 */}
          <div className="flex justify-end gap-4 pt-4 border-t">
            <Button variant="outline" onClick={() => navigate(-1)} disabled={isSubmitting}>
              取消
            </Button>
            <Button data-testid="create-project-submit" onClick={handleSubmit} disabled={isSubmitting}>
              {isSubmitting ? (
                <>
                  <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                  创建中...
                </>
              ) : (
                '创建并启动'
              )}
            </Button>
          </div>
        </div>
      </main>
    </div>
  )
}
