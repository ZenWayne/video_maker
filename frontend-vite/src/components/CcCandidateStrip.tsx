// components/CcCandidateStrip.tsx
// CC 人物校准候选条：当前尾帧 → 候选对比 → 采纳/删除/再校准
import { ArrowRight, Loader2, RefreshCw, UserCheck } from 'lucide-react'
import type { Shot } from '@/lib/types'

interface Props {
  shot: Shot
  currentLastFrame: string | null
  onAdopt: (candidateId: string) => void
  onDelete: (candidateId: string) => void
  onRecalibrate: () => void
}

export function CcCandidateStrip({ shot, currentLastFrame, onAdopt, onDelete, onRecalibrate }: Props) {
  const ccCands = (shot.image_candidates ?? []).filter(c => c.slot === 'cc' && !c.adopted_at)
  if (ccCands.length === 0) return null
  const pending = ccCands.filter(c => c.status === 'done').length

  return (
    <div className="space-y-2.5 rounded-lg border border-zinc-200 bg-white p-3">
      <div className="flex items-center justify-between">
        <span className="flex items-center gap-1.5 text-[13px] font-semibold text-zinc-700">
          <UserCheck className="h-3.5 w-3.5 text-zinc-600" /> 人物校准候选
        </span>
        {pending > 0 && (
          <span className="rounded-md bg-yellow-100 px-2 py-0.5 text-xs font-medium text-yellow-700">
            {pending} 张待采纳
          </span>
        )}
      </div>
      <div className="flex items-center gap-2.5">
        <div className="flex flex-col items-center gap-1">
          <div className="h-[135px] w-[76px] overflow-hidden rounded-md bg-zinc-300">
            {currentLastFrame && <img src={currentLastFrame} className="h-full w-full object-cover" />}
          </div>
          <span className="text-[11px] text-zinc-500">当前尾帧</span>
        </div>
        <ArrowRight className="h-4 w-4 shrink-0 text-zinc-400" />
        {ccCands.map(c => (
          <div key={c.id} className="flex flex-col items-center gap-1">
            {c.status === 'generating' ? (
              <div className="flex h-[135px] w-[76px] items-center justify-center rounded-md border border-zinc-200 bg-zinc-50">
                <Loader2 className="h-4 w-4 animate-spin text-blue-600" />
              </div>
            ) : c.status === 'failed' ? (
              <div className="flex h-[135px] w-[76px] items-center justify-center rounded-md border border-red-200 bg-red-50 p-1 text-center text-[11px] text-red-600" title={c.error ?? ''}>
                失败
              </div>
            ) : (
              <div className="h-[135px] w-[76px] overflow-hidden rounded-md border-2 border-blue-500">
                <img src={c.file_path ?? ''} className="h-full w-full object-cover" />
              </div>
            )}
            <div className="flex gap-2 text-xs">
              {c.status === 'done' && (
                <button className="font-semibold text-blue-600" onClick={() => onAdopt(c.id)}>采纳</button>
              )}
              {c.status !== 'generating' && (
                <button className="text-red-600" onClick={() => onDelete(c.id)}>删除</button>
              )}
            </div>
          </div>
        ))}
        <button
          onClick={onRecalibrate}
          className="flex h-[135px] w-[76px] flex-col items-center justify-center gap-1 rounded-md border border-dashed border-zinc-300 text-zinc-400"
        >
          <RefreshCw className="h-4 w-4" />
          <span className="text-[11px]">再校准一次</span>
        </button>
      </div>
      <p className="text-xs text-zinc-400">采纳后替换本镜尾帧（自动保留 pre-CC 备份，可随时还原）</p>
    </div>
  )
}
