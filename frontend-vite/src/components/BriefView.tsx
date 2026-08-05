// components/BriefView.tsx - 创作简报展示（shots.pen Ⓓ）

import { CheckCircle2, XCircle, Download, Link2, AlertTriangle } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Separator } from '@/components/ui/separator'
import type { CreationBrief } from '@/lib/types'

interface BriefViewProps {
  brief: CreationBrief
  onAttach?: () => void
  onExport?: () => void
}

const STRUCTURE_TILES: { key: keyof CreationBrief['script_structure']; label: string }[] = [
  { key: 'pacing', label: '节奏' },
  { key: 'emotion', label: '情绪' },
  { key: 'info_gap', label: '信息差' },
  { key: 'cta', label: 'CTA' },
]

export function BriefView({ brief, onAttach, onExport }: BriefViewProps) {
  const { sample_stats, hook_strategy, script_structure } = brief

  return (
    <div data-testid="brief-view" className="space-y-4">
      {/* Niche summary */}
      <Card>
        <CardHeader>
          <CardTitle>赛道摘要</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-zinc-700 leading-relaxed">{brief.niche_summary}</p>
        </CardContent>
      </Card>

      {/* Sample stats */}
      <Card>
        <CardHeader>
          <CardTitle>样本统计</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-2">
          <Badge variant="secondary" className="bg-zinc-100 text-zinc-700">
            样本数 {sample_stats.sample_n}
          </Badge>
          <Badge variant="secondary" className="bg-zinc-100 text-zinc-700">
            无人声占比 {Math.round(sample_stats.no_speech_pct * 100)}%
          </Badge>
          {sample_stats.sample_warning && (
            <Badge variant="secondary" className="bg-amber-100 text-amber-700 gap-1">
              <AlertTriangle className="w-3 h-3" />
              {sample_stats.sample_warning}
            </Badge>
          )}
        </CardContent>
      </Card>

      {/* Hook strategy */}
      <Card>
        <CardHeader>
          <CardTitle>钩子策略</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap gap-2">
            {hook_strategy.common_hook_types.map((t) => (
              <Badge key={t} variant="secondary" className="bg-blue-100 text-blue-700">
                {t}
              </Badge>
            ))}
          </div>
          {hook_strategy.example_hooks.length > 0 && (
            <ul className="space-y-1.5">
              {hook_strategy.example_hooks.map((h, i) => (
                <li key={i} className="text-sm text-zinc-600 italic border-l-2 border-zinc-200 pl-2">
                  “{h}”
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      {/* Script structure - 2x2 */}
      <Card>
        <CardHeader>
          <CardTitle>脚本结构</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {STRUCTURE_TILES.map(({ key, label }) => (
              <div key={key} className="rounded-lg border border-zinc-200 p-3">
                <p className="text-xs font-medium text-zinc-500 mb-1">{label}</p>
                <p className="text-sm text-zinc-800">{script_structure[key]}</p>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Do / Don't */}
      <Card>
        <CardHeader>
          <CardTitle>做到 / 避免</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <ul className="space-y-1.5">
              {brief.do.map((d, i) => (
                <li key={i} className="flex items-start gap-2 text-sm text-zinc-700">
                  <CheckCircle2 className="w-4 h-4 text-green-600 shrink-0 mt-0.5" />
                  {d}
                </li>
              ))}
            </ul>
            <ul className="space-y-1.5">
              {brief.dont.map((d, i) => (
                <li key={i} className="flex items-start gap-2 text-sm text-zinc-700">
                  <XCircle className="w-4 h-4 text-red-600 shrink-0 mt-0.5" />
                  {d}
                </li>
              ))}
            </ul>
          </div>
        </CardContent>
      </Card>

      {/* Screenwriter directives - blue highlighted */}
      <Card className="bg-blue-50 ring-1 ring-blue-200">
        <CardHeader>
          <div className="flex items-center gap-2">
            <CardTitle>编剧指令</CardTitle>
            <Badge variant="secondary" className="bg-blue-600 text-white">
              → screenwriter
            </Badge>
          </div>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-blue-900 leading-relaxed whitespace-pre-wrap">
            {brief.screenwriter_directives}
          </p>
        </CardContent>
      </Card>

      <Separator />

      {/* Actions */}
      <div className="flex items-center justify-end gap-2">
        {onExport && (
          <Button variant="outline" onClick={onExport}>
            <Download className="w-4 h-4 mr-2" />
            导出 Markdown
          </Button>
        )}
        <Button data-testid="brief-attach-btn" onClick={onAttach}>
          <Link2 className="w-4 h-4 mr-2" />
          挂载到项目
        </Button>
      </div>
    </div>
  )
}

export default BriefView
