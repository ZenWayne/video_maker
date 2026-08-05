// components/AnalysisProgress.tsx - 三步进度器 + 逐样本行（shots.pen Ⓒ）

import { Check } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import { analysisSteps, SAMPLE_STATUS_LABELS, SAMPLE_STATUS_COLORS } from '@/lib/analysisStatus'
import type { ContentAnalysis, ReferenceSample } from '@/lib/types'

interface AnalysisProgressProps {
  analysis: ContentAnalysis
}

function stepCircleClasses(state: 'done' | 'active' | 'pending') {
  if (state === 'done') return 'bg-green-600 border-green-600 text-white'
  if (state === 'active') return 'bg-blue-600 border-blue-600 text-white'
  return 'bg-white border-zinc-300 text-zinc-400'
}

function Stepper({ status }: { status: ContentAnalysis['status'] }) {
  const steps = analysisSteps(status)
  return (
    <div data-testid="analysis-stepper" className="flex items-center">
      {steps.map((step, i) => (
        <div key={step.key} className="flex items-center flex-1 last:flex-none">
          <div className="flex flex-col items-center gap-1">
            <div
              className={cn(
                'w-8 h-8 rounded-full border-2 flex items-center justify-center text-sm font-medium',
                stepCircleClasses(step.state)
              )}
            >
              {step.state === 'done' ? <Check className="w-4 h-4" /> : i + 1}
            </div>
            <span
              className={cn(
                'text-xs whitespace-nowrap',
                step.state === 'active' ? 'text-blue-700 font-medium' : 'text-zinc-500'
              )}
            >
              {step.label}
            </span>
          </div>
          {i < steps.length - 1 && (
            <div
              className={cn(
                'h-0.5 flex-1 mx-2',
                step.state === 'done' ? 'bg-green-600' : 'bg-zinc-200'
              )}
            />
          )}
        </div>
      ))}
    </div>
  )
}

function formatDuration(sample: ReferenceSample): string {
  // 时长信息目前不在 ReferenceSample 上，占位显示语言/文件名即可
  return sample.language ? sample.language : '未知语言'
}

function sampleFilename(sample: ReferenceSample): string {
  const parts = sample.video_path.split('/')
  return parts[parts.length - 1] || sample.video_path
}

function sampleCopy(sample: ReferenceSample): string {
  if (sample.status === 'failed') {
    return sample.error_message || '（转写失败）'
  }
  return sample.hook_text || '（等待转写）'
}

function SampleRow({ sample }: { sample: ReferenceSample }) {
  const skipped = sample.has_speech === false
  return (
    <div
      data-testid={`sample-row-${sample.id}`}
      className={cn(
        'flex items-center gap-3 p-3 rounded-lg border',
        skipped ? 'bg-zinc-50 border-zinc-100 opacity-60' : 'bg-white border-zinc-200'
      )}
    >
      {/* thumbnail placeholder */}
      <div className="w-14 h-14 shrink-0 rounded bg-zinc-200 flex items-center justify-center text-[10px] text-zinc-400">
        视频
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-zinc-800 truncate">{sampleFilename(sample)}</span>
          <span className="text-xs text-zinc-400 shrink-0">{formatDuration(sample)}</span>
        </div>
        {skipped ? (
          <p className="text-xs text-zinc-400 mt-0.5">无人声・跳过</p>
        ) : (
          <p className="text-xs text-zinc-500 truncate mt-0.5">
            {sampleCopy(sample)}
          </p>
        )}
      </div>
      <Badge variant="secondary" className={cn('shrink-0', SAMPLE_STATUS_COLORS[sample.status])}>
        {SAMPLE_STATUS_LABELS[sample.status]}
      </Badge>
    </div>
  )
}

export function AnalysisProgress({ analysis }: AnalysisProgressProps) {
  const samples = analysis.samples
  const total = samples.length
  const withSpeech = samples.filter((s) => s.has_speech !== false)
  const transcribedCount = withSpeech.filter((s) => s.status === 'transcribed').length
  const noSpeechCount = samples.filter((s) => s.has_speech === false).length
  const noSpeechPct = total > 0 ? Math.round((noSpeechCount / total) * 100) : 0

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="py-2">
          <Stepper status={analysis.status} />
        </CardContent>
      </Card>

      <p className="text-sm text-zinc-600">
        已转写 {transcribedCount}/{total} · 无人声占比 {noSpeechPct}%
      </p>

      <div className="space-y-2">
        {samples.map((s) => (
          <SampleRow key={s.id} sample={s} />
        ))}
      </div>
    </div>
  )
}

export default AnalysisProgress
