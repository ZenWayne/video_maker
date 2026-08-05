// pages/NewAnalysisPage.tsx - 新建内容分析页

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, Loader2, Info } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { UploadZone } from '@/components/UploadZone'
import { api } from '@/lib/api'
import { useStore } from '@/lib/state'

export default function NewAnalysisPage() {
  const navigate = useNavigate()
  const { addToast } = useStore()
  const [isSubmitting, setIsSubmitting] = useState(false)

  const [title, setTitle] = useState('')
  const [regionHint, setRegionHint] = useState('')
  const [videos, setVideos] = useState<File[]>([])

  const handleSubmit = async () => {
    if (!title.trim()) {
      addToast({ type: 'error', message: '请输入分析名称' })
      return
    }
    if (videos.length === 0) {
      addToast({ type: 'error', message: '请上传至少一条视频样本' })
      return
    }

    setIsSubmitting(true)
    try {
      const a = await api.createAnalysis({
        title: title.trim(),
        regionHint: regionHint.trim() || undefined,
        files: videos,
      })
      navigate(`/analyses/${a.id}`)
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '创建分析失败',
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
          <h1 className="text-lg font-medium">新建内容分析</h1>
        </div>
      </header>

      {/* Form */}
      <main className="max-w-3xl mx-auto px-4 py-8">
        <div className="bg-white rounded-lg shadow-sm p-6 space-y-6">
          {/* 分析名称 */}
          <div className="space-y-2">
            <Label htmlFor="analysis-title">
              分析名称 <span className="text-red-500">*</span>
            </Label>
            <Input
              data-testid="analysis-title-input"
              id="analysis-title"
              placeholder="给这次分析起个名字"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </div>

          {/* 目标语言（可选） */}
          <div className="space-y-2">
            <Label htmlFor="analysis-lang">目标语言（可选）</Label>
            <Input
              data-testid="analysis-lang-input"
              id="analysis-lang"
              placeholder="例如 zh、en"
              value={regionHint}
              onChange={(e) => setRegionHint(e.target.value)}
            />
            <p className="text-xs text-zinc-400">
              非模型支持的语言码（如 en-US、美国）会被拒绝
            </p>
          </div>

          {/* 爆款视频样本 */}
          <div className="space-y-2">
            <Label>
              爆款视频样本（建议 &ge;3 条）
            </Label>
            <div data-testid="analysis-upload">
              <UploadZone kind="video" maxFiles={20} value={videos} onChange={setVideos} />
            </div>
          </div>

          {/* 合规说明 */}
          <div className="flex gap-2 rounded-lg border border-blue-200 bg-blue-50 p-4 text-sm text-blue-700">
            <Info className="w-4 h-4 flex-shrink-0 mt-0.5" />
            <p>
              只分析口播转写文本，不存储、不分发原视频内容；音频转写后即删。
            </p>
          </div>

          {/* 提交按钮 */}
          <div className="flex justify-end gap-4 pt-4 border-t">
            <Button variant="outline" onClick={() => navigate(-1)} disabled={isSubmitting}>
              取消
            </Button>
            <Button data-testid="create-analysis-submit" onClick={handleSubmit} disabled={isSubmitting}>
              {isSubmitting ? (
                <>
                  <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                  分析中...
                </>
              ) : (
                '开始分析'
              )}
            </Button>
          </div>
        </div>
      </main>
    </div>
  )
}
