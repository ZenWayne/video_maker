// app/page.tsx - 项目列表首页

import { useEffect, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { Plus, Search, Filter, MoreVertical, Trash2, FolderOpen } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { UserBadge } from '@/components/UserBadge'
import { ProgressStream } from '@/components/ProgressStream'
import { api } from '@/lib/api'
import { useStore } from '@/lib/state'
import type { Project, ProjectStatus } from '@/lib/types'

const statusLabels: Record<ProjectStatus, string> = {
  draft: '草稿',
  scripting: '生成脚本中',
  script_review: '脚本审批中',
  shot_generating: '生成分镜中',
  shot_review: '分镜审批中',
  exporting: '导出中',
  exported: '已完成',
  failed: '失败',
}

const statusColors: Record<ProjectStatus, string> = {
  draft: 'bg-zinc-100 text-zinc-600',
  scripting: 'bg-blue-100 text-blue-700',
  script_review: 'bg-yellow-100 text-yellow-700',
  shot_generating: 'bg-blue-100 text-blue-700',
  shot_review: 'bg-yellow-100 text-yellow-700',
  exporting: 'bg-blue-100 text-blue-700',
  exported: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
}

export default function HomePage() {
  const navigate = useNavigate()
  const { addToast, userName } = useStore()
  const [projects, setProjects] = useState<Project[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [searchQuery, setSearchQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState<ProjectStatus | ''>('')

  // 获取项目列表
  const fetchProjects = useCallback(async () => {
    try {
      const data = await api.listProjects({
        status: statusFilter || undefined,
      })
      setProjects(data)
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '获取项目列表失败',
      })
    } finally {
      setIsLoading(false)
    }
  }, [statusFilter, addToast])

  // 初始加载和轮询
  useEffect(() => {
    fetchProjects()
    const interval = setInterval(fetchProjects, 5000)
    return () => clearInterval(interval)
  }, [fetchProjects])

  // 删除项目
  const handleDelete = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation()
    if (!confirm('确定要删除这个项目吗？此操作不可恢复。')) return

    try {
      await api.deleteProject(id)
      setProjects(projects.filter((p) => p.id !== id))
      addToast({ type: 'success', message: '项目已删除' })
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '删除失败',
      })
    }
  }

  // 打开项目
  const handleOpenProject = (id: string) => {
    navigate(`/projects/${id}`)
  }

  // 过滤项目
  const filteredProjects = projects.filter((p) =>
    p.title.toLowerCase().includes(searchQuery.toLowerCase())
  )

  // 检查是否需要设置用户名
  useEffect(() => {
    if (!userName) {
      addToast({
        type: 'info',
        message: '请先点击右上角设置用户名',
      })
    }
  }, [userName, addToast])

  return (
    <div className="min-h-screen bg-zinc-50">
      {/* Header */}
      <header className="bg-white border-b sticky top-0 z-10">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
          <h1 className="text-xl font-semibold text-zinc-900">视频制作工具</h1>
          <div className="flex items-center gap-4">
            <UserBadge />
            <Button data-testid="new-project-button" onClick={() => navigate('/projects/new')}>
              <Plus className="w-4 h-4 mr-2" />
              新建项目
            </Button>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Filters */}
        <div className="flex flex-col sm:flex-row gap-4 mb-6">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-zinc-400" />
            <Input
              data-testid="search-input"
              placeholder="搜索项目标题..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="pl-10"
            />
          </div>
          <div className="flex items-center gap-2">
            <Filter className="w-4 h-4 text-zinc-400" />
            <select
              data-testid="status-filter"
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value as ProjectStatus | '')}
              className="h-10 rounded-md border border-input bg-background px-3 py-2 text-sm"
            >
              <option value="">所有状态</option>
              <option value="draft">草稿</option>
              <option value="scripting">生成脚本中</option>
              <option value="script_review">脚本审批中</option>
              <option value="shot_generating">生成分镜中</option>
              <option value="shot_review">分镜审批中</option>
              <option value="exporting">导出中</option>
              <option value="exported">已完成</option>
              <option value="failed">失败</option>
            </select>
          </div>
        </div>

        {/* Project Grid */}
        {isLoading ? (
          <div className="flex items-center justify-center h-64">
            <div className="w-8 h-8 border-4 border-blue-500 border-t-transparent rounded-full animate-spin" />
          </div>
        ) : filteredProjects.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-64 text-zinc-500">
            <FolderOpen className="w-12 h-12 mb-4 text-zinc-300" />
            <p>暂无项目</p>
            <Button
              variant="outline"
              className="mt-4"
              onClick={() => navigate('/projects/new')}
            >
              创建第一个项目
            </Button>
          </div>
        ) : (
          <div data-testid="project-list" className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {filteredProjects.map((project) => (
              <Card
                key={project.id}
                data-testid="project-card"
                className="cursor-pointer hover:shadow-md transition-shadow"
                onClick={() => handleOpenProject(project.id)}
              >
                <CardHeader className="pb-3">
                  <div className="flex items-start justify-between">
                    <div className="flex-1 min-w-0">
                      <h3 className="font-medium text-zinc-900 truncate">{project.title}</h3>
                      <p className="text-sm text-zinc-500 mt-1">
                        {project.creator_name} · {new Date(project.created_at).toLocaleDateString()}
                      </p>
                    </div>
                    <DropdownMenu>
                      <DropdownMenuTrigger
                        className="p-2 hover:bg-zinc-100 rounded-md"
                        onClick={(e: React.MouseEvent) => e.stopPropagation()}
                      >
                        <MoreVertical className="w-4 h-4" />
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem onClick={() => handleOpenProject(project.id)}>
                          <FolderOpen className="w-4 h-4 mr-2" />打开
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          className="text-red-600"
                          onClick={(e) => handleDelete(project.id, e)}
                        >
                          <Trash2 className="w-4 h-4 mr-2" />删除
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                  <Badge className={statusColors[project.status]} variant="secondary">
                    {statusLabels[project.status]}
                  </Badge>
                </CardHeader>
                <CardContent>
                  {/* 缩略图或进度 */}
                  {project.status === 'exported' && project.final_video_path ? (
                    <div className={`${project.aspect_ratio === '9:16' ? 'aspect-[9/16]' : 'aspect-video'} bg-zinc-100 rounded-lg overflow-hidden`}>
                      <video
                        src={api.finalVideoUrl(project.id)}
                        className="w-full h-full object-cover"
                      />
                    </div>
                  ) : ['scripting', 'shot_generating', 'exporting'].includes(project.status) ? (
                    <ProgressStream projectId={project.id} />
                  ) : (
                    <div className={`${project.aspect_ratio === '9:16' ? 'aspect-[9/16]' : 'aspect-video'} bg-zinc-100 rounded-lg flex items-center justify-center`}>
                      <span className="text-zinc-400 text-sm">{project.theme_text.slice(0, 50)}...</span>
                    </div>
                  )}
                </CardContent>
              </Card>
            ))}
          </div>
        )}
      </main>
    </div>
  )
}
