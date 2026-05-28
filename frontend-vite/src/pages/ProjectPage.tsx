// pages/ProjectPage.tsx - 项目入口（智能跳转）

import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Loader2, AlertCircle, RotateCcw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { api } from '@/lib/api'
import { useStore } from '@/lib/state'
import type { ProjectStatus } from '@/lib/types'

export default function ProjectPage() {
  const navigate = useNavigate()
  const { id: projectId } = useParams<{ id: string }>()
  const { addToast, setCurrentProject } = useStore()
  const [isLoading, setIsLoading] = useState(true)
  const [projectStatus, setProjectStatus] = useState<ProjectStatus | null>(null)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  useEffect(() => {
    if (!projectId) return

    const checkStatus = async () => {
      try {
        const project = await api.getProject(projectId)
        setCurrentProject(project)
        setProjectStatus(project.status)
        setErrorMessage(project.error_message)

        // 根据状态跳转
        switch (project.status) {
          case 'draft':
            // 停留在当前页，显示开始生成按钮
            setIsLoading(false)
            break
          case 'scripting':
            navigate(`/projects/${projectId}/script`, { replace: true })
            break
          case 'script_review':
          case 'shot_generating':
          case 'shot_review':
          case 'shots_ready':
            navigate(`/projects/${projectId}/shots`, { replace: true })
            break
          case 'exporting':
          case 'exported':
            navigate(`/projects/${projectId}/export`, { replace: true })
            break
          case 'failed':
            setIsLoading(false)
            break
        }
      } catch (error) {
        addToast({
          type: 'error',
          message: error instanceof Error ? error.message : '获取项目失败',
        })
        setIsLoading(false)
      }
    }

    checkStatus()
  }, [projectId, navigate, setCurrentProject, addToast])

  const handleReset = async () => {
    if (!projectId) return
    try {
      await api.resetProject(projectId)
      addToast({ type: 'success', message: '项目已重置' })
      navigate(`/projects/${projectId}`, { replace: true })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '重置失败',
      })
    }
  }

  const handleStart = async () => {
    if (!projectId) return
    try {
      await api.startPipeline(projectId)
      navigate(`/projects/${projectId}/shots`, { replace: true })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '启动失败',
      })
    }
  }

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="flex flex-col items-center gap-4">
          <Loader2 className="w-8 h-8 animate-spin text-blue-600" />
          <p className="text-zinc-500">正在加载项目...</p>
        </div>
      </div>
    )
  }

  // FAILED 状态
  if (projectStatus === 'failed') {
    return (
      <div className="min-h-screen flex items-center justify-center bg-zinc-50">
        <div className="bg-white rounded-lg shadow-sm p-8 max-w-md w-full text-center">
          <AlertCircle className="w-12 h-12 text-red-500 mx-auto mb-4" />
          <h2 className="text-xl font-semibold text-zinc-900 mb-2">项目处理失败</h2>
          {errorMessage && (
            <p className="text-sm text-zinc-600 mb-6 bg-zinc-50 p-3 rounded">
              {errorMessage}
            </p>
          )}
          <div className="flex gap-3 justify-center">
            <Button variant="outline" onClick={() => navigate('/')}>
              返回首页
            </Button>
            <Button onClick={handleReset}>
              <RotateCcw className="w-4 h-4 mr-2" />
              重置项目
            </Button>
          </div>
        </div>
      </div>
    )
  }

  // DRAFT 状态
  return (
    <div className="min-h-screen flex items-center justify-center bg-zinc-50">
      <div className="bg-white rounded-lg shadow-sm p-8 max-w-md w-full text-center">
        <h2 className="text-xl font-semibold text-zinc-900 mb-2">项目已创建</h2>
        <p className="text-zinc-500 mb-6">点击下方按钮开始生成脚本</p>
        <div className="flex gap-3 justify-center">
          <Button variant="outline" onClick={() => navigate('/')}>
            返回首页
          </Button>
          <Button onClick={handleStart}>开始生成</Button>
        </div>
      </div>
    </div>
  )
}
