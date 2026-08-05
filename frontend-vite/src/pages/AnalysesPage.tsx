// pages/AnalysesPage.tsx - 内容分析列表页

import { useEffect, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, Plus, Search, FileText } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { api } from '@/lib/api'
import { useStore } from '@/lib/state'
import { ANALYSIS_STATUS_LABELS, ANALYSIS_STATUS_COLORS, parseBrief } from '@/lib/analysisStatus'
import type { ContentAnalysis, ContentAnalysisStatus } from '@/lib/types'

const STATUS_PLACEHOLDER: Record<ContentAnalysisStatus, string> = {
  uploading: '样本上传中，等待转写…',
  transcribing: '正在转写口播文案…',
  analyzing: '正在联合归纳创作简报…',
  completed: '',
  failed: '分析失败',
}

export default function AnalysesPage() {
  const navigate = useNavigate()
  const { addToast } = useStore()
  const [analyses, setAnalyses] = useState<ContentAnalysis[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [searchQuery, setSearchQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState<ContentAnalysisStatus | ''>('')

  // 获取分析列表
  const fetchAnalyses = useCallback(async () => {
    try {
      const data = await api.listAnalyses()
      setAnalyses(data)
    } catch (error) {
      addToast({
        type: 'error',
        message: error instanceof Error ? error.message : '获取分析列表失败',
      })
    } finally {
      setIsLoading(false)
    }
  }, [addToast])

  // 初始加载和轮询
  useEffect(() => {
    fetchAnalyses()
    const interval = setInterval(fetchAnalyses, 5000)
    return () => clearInterval(interval)
  }, [fetchAnalyses])

  // 过滤
  const filteredAnalyses = analyses.filter(
    (a) =>
      a.title.toLowerCase().includes(searchQuery.toLowerCase()) &&
      (!statusFilter || a.status === statusFilter)
  )

  const handleOpenAnalysis = (id: string) => {
    navigate(`/analyses/${id}`)
  }

  return (
    <div className="min-h-screen bg-zinc-50">
      {/* Header */}
      <header
        data-testid="analyses-page"
        className="bg-white border-b sticky top-0 z-10"
      >
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Button variant="ghost" size="icon" onClick={() => navigate('/')}>
              <ArrowLeft className="w-4 h-4" />
            </Button>
            <h1 className="text-xl font-semibold text-zinc-900">内容分析</h1>
          </div>
          <Button data-testid="new-analysis-btn" onClick={() => navigate('/analyses/new')}>
            <Plus className="w-4 h-4 mr-2" />
            新建分析
          </Button>
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
              placeholder="搜索分析标题..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="pl-10"
            />
          </div>
          <div className="flex items-center gap-2">
            <select
              data-testid="status-filter"
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value as ContentAnalysisStatus | '')}
              className="h-10 rounded-md border border-input bg-background px-3 py-2 text-sm"
            >
              <option value="">所有状态</option>
              <option value="uploading">上传中</option>
              <option value="transcribing">转写中</option>
              <option value="analyzing">归纳中</option>
              <option value="completed">已完成</option>
              <option value="failed">失败</option>
            </select>
          </div>
        </div>

        {/* Analysis Grid */}
        {isLoading ? (
          <div className="flex items-center justify-center h-64">
            <div className="w-8 h-8 border-4 border-blue-500 border-t-transparent rounded-full animate-spin" />
          </div>
        ) : filteredAnalyses.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-64 text-zinc-500">
            <FileText className="w-12 h-12 mb-4 text-zinc-300" />
            <p>暂无内容分析</p>
            <Button variant="outline" className="mt-4" onClick={() => navigate('/analyses/new')}>
              创建第一个分析
            </Button>
          </div>
        ) : (
          <div
            data-testid="analysis-list"
            className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4"
          >
            {filteredAnalyses.map((a) => {
              const brief = a.status === 'completed' ? parseBrief(a.brief_json) : null
              const summary = brief?.niche_summary || STATUS_PLACEHOLDER[a.status]
              return (
                <Card
                  key={a.id}
                  data-testid={`analysis-card-${a.id}`}
                  className="cursor-pointer hover:shadow-md transition-shadow"
                  onClick={() => handleOpenAnalysis(a.id)}
                >
                  <CardHeader className="pb-3">
                    <div className="flex items-start justify-between gap-2">
                      <h3 className="font-medium text-zinc-900 truncate flex-1 min-w-0">{a.title}</h3>
                      <Badge variant="secondary" className={ANALYSIS_STATUS_COLORS[a.status]}>
                        {ANALYSIS_STATUS_LABELS[a.status]}
                      </Badge>
                    </div>
                    <p className="text-sm text-zinc-500 mt-1">
                      {a.samples.length} 个样本 · {new Date(a.created_at).toLocaleDateString()}
                    </p>
                  </CardHeader>
                  <CardContent>
                    <p className="text-sm text-zinc-600 line-clamp-2 min-h-[2.5rem]">{summary}</p>
                    <p className="text-sm text-blue-600 mt-2">查看简报 &rsaquo;</p>
                  </CardContent>
                </Card>
              )
            })}
          </div>
        )}
      </main>
    </div>
  )
}
