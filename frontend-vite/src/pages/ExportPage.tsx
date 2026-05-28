// pages/ExportPage.tsx - 导出页

import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  ArrowLeft,
  Download,
  Loader2,
  CheckCircle,
  ArrowLeftCircle,
  FileVideo,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Card } from '@/components/ui/card'
import { ProgressStream } from '@/components/ProgressStream'
import { api } from '@/lib/api'
import { useStore } from '@/lib/state'
import type { ProjectStatus } from '@/lib/types'

export default function ExportPage() {
  const navigate = useNavigate()
  const { id: projectId } = useParams<{ id: string }>()
  const { addToast, currentProject, setCurrentProject } = useStore()

  const [status, setStatus] = useState<ProjectStatus>('exporting')
  const [finalVideoPath, setFinalVideoPath] = useState<string | null>(null)

  const handleSSEEvent = useCallback((type: string, data: unknown) => {
    if (type === 'export_done') {
      setStatus('exported')
      setFinalVideoPath((data as { final_video_path: string }).final_video_path)
    }
  }, [])

  // 获取项目详情
  useEffect(() => {
    if (!projectId) return

    const fetchProject = async () => {
      try {
        const project = await api.getProject(projectId)
        setCurrentProject(project)
        setStatus(project.status as ProjectStatus)
        setFinalVideoPath(project.final_video_path)
      } catch (error) {
        addToast({
          type: 'error',
          message: error instanceof Error ? error.message : '获取项目失败',
        })
      }
    }

    fetchProject()
  }, [projectId, setCurrentProject, addToast])

  // 下载视频
  const handleDownload = () => {
    if (!projectId || !finalVideoPath) return
    const url = api.finalVideoUrl(projectId)
    window.open(url, '_blank')
  }

  // 退回分镜审批
  const handleBackToShots = () => {
    if (!projectId) return
    navigate(`/projects/${projectId}/shots`)
  }

  // 退回脚本审批
  const handleBackToScript = async () => {
    if (!projectId) return
    if (!confirm('确定要退回脚本审批吗？')) return
    try {
      await api.resetToScript(projectId)
      navigate(`/projects/${projectId}/script`)
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '操作失败',
      })
    }
  }

  // EXPORTING 状态
  if (status === 'exporting') {
    return (
      <div className="min-h-screen bg-zinc-50">
        <header className="bg-white border-b">
          <div className="max-w-5xl mx-auto px-4 h-14 flex items-center gap-4">
            <Button variant="ghost" size="icon" onClick={() => navigate('/')}>
              <ArrowLeft className="w-5 h-5" />
            </Button>
            <h1 className="text-lg font-medium">{currentProject?.title}</h1>
            <Badge variant="secondary" className="bg-blue-100 text-blue-700">导出中</Badge>
          </div>
        </header>

        <main className="max-w-5xl mx-auto px-4 py-8">
          <div data-testid="export-progress" className="flex flex-col items-center justify-center py-16">
            <Loader2 className="w-12 h-12 animate-spin text-blue-600 mb-4" />
            <p className="text-zinc-500">正在导出视频，请稍候...</p>
          </div>
          {projectId && (
            <ProgressStream
              projectId={projectId}
              onEvent={handleSSEEvent}
            />
          )}
        </main>
      </div>
    )
  }

  // EXPORTED 状态
  return (
    <div className="min-h-screen bg-zinc-50">
      {/* Header */}
      <header className="bg-white border-b">
        <div className="max-w-5xl mx-auto px-4 h-14 flex items-center gap-4">
          <Button variant="ghost" size="icon" onClick={() => navigate('/')}>
            <ArrowLeft className="w-5 h-5" />
          </Button>
          <h1 className="text-lg font-medium">{currentProject?.title}</h1>
          <Badge variant="secondary" className="bg-green-100 text-green-700">已完成</Badge>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-4 py-8">
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Video Player */}
          <div className="lg:col-span-2">
            <Card className="overflow-hidden">
              {finalVideoPath ? (
                <div className={`${currentProject?.aspect_ratio === '9:16' ? 'aspect-[9/16]' : 'aspect-video'} bg-zinc-900`}>
                  <video
                    src={finalVideoPath}
                    controls
                    className="w-full h-full"
                  />
                </div>
              ) : (
                <div className={`${currentProject?.aspect_ratio === '9:16' ? 'aspect-[9/16]' : 'aspect-video'} bg-zinc-100 flex items-center justify-center`}>
                  <FileVideo className="w-16 h-16 text-zinc-300" />
                </div>
              )}
            </Card>
          </div>

          {/* Info Panel */}
          <div className="space-y-4">
            <Card className="p-6">
              <div className="flex items-center gap-3 mb-4">
                <div className="w-10 h-10 rounded-full bg-green-100 flex items-center justify-center">
                  <CheckCircle className="w-5 h-5 text-green-600" />
                </div>
                <div>
                  <h2 className="font-medium">导出成功</h2>
                  <p className="text-sm text-zinc-500">视频已准备就绪</p>
                </div>
              </div>

              <Button
                data-testid="download-video-button"
                className="w-full"
                size="lg"
                onClick={handleDownload}
                disabled={!finalVideoPath}
              >
                <Download className="w-5 h-5 mr-2" />
                下载 MP4
              </Button>
            </Card>

            <Card className="p-6">
              <h3 className="font-medium mb-4">项目信息</h3>
              <div className="space-y-3 text-sm">
                <div className="flex justify-between">
                  <span className="text-zinc-500">标题</span>
                  <span className="text-right">{currentProject?.title}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-zinc-500">创建者</span>
                  <span>{currentProject?.creator_name}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-zinc-500">创建时间</span>
                  <span>
                    {currentProject?.created_at
                      ? new Date(currentProject.created_at).toLocaleString()
                      : '-'}
                  </span>
                </div>
              </div>
            </Card>

            <Card className="p-6">
              <h3 className="font-medium mb-4">返回修改</h3>
              <div className="space-y-2">
                <Button
                  variant="outline"
                  className="w-full justify-start"
                  onClick={handleBackToShots}
                >
                  <ArrowLeftCircle className="w-4 h-4 mr-2" />
                  退回分镜审批
                </Button>
                <Button
                  variant="outline"
                  className="w-full justify-start"
                  onClick={handleBackToScript}
                >
                  <ArrowLeftCircle className="w-4 h-4 mr-2" />
                  退回脚本审批
                </Button>
              </div>
            </Card>
          </div>
        </div>
      </main>
    </div>
  )
}
