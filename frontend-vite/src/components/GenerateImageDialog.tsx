// components/GenerateImageDialog.tsx
// 统一图片生成弹窗：槽位切换 / 自定义提示词(可选,缺省自动) / 参考图勾选+临时上传 / 候选画廊
import { useEffect, useMemo, useRef, useState } from 'react'
import { Loader2, Plus, Sparkles, Check, X } from 'lucide-react'
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { api } from '@/lib/api'
import type { ImageCandidate, ProjectDetail, Shot } from '@/lib/types'

interface Props {
  project: ProjectDetail
  shot: Shot
  slot: 'first_frame' | 'tail_frame'
  open: boolean
  onOpenChange: (open: boolean) => void
  onChanged: () => void
}

const SLOT_LABEL = { first_frame: '首帧', tail_frame: '尾帧' } as const

// 参考图/候选图均通过 /api/media 静态挂载访问，后端返回的是原始 storage_path
// （与 ReferenceAssetsPanel.tsx 的 refMediaUrl 约定一致）
const refMediaUrl = (storagePath: string) =>
  `/api/media/${storagePath.replace(/^\/?storage\//, '')}`

export function GenerateImageDialog({ project, shot, slot: initialSlot, open, onOpenChange, onChanged }: Props) {
  const [slot, setSlot] = useState<'first_frame' | 'tail_frame'>(initialSlot)
  const [prompt, setPrompt] = useState('')
  // 默认勾选所有 character 参考图（与后端缺省一致）
  const [checkedRefIds, setCheckedRefIds] = useState<Set<string>>(
    () => new Set(project.reference_images.filter(r => r.kind === 'character').map(r => r.id)),
  )
  const [tempFiles, setTempFiles] = useState<File[]>([])
  const [submitting, setSubmitting] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  const candidates = useMemo(
    () => (shot.image_candidates ?? []).filter(c => c.slot === slot),
    [shot.image_candidates, slot],
  )

  // 临时上传文件的预览 URL：随 tempFiles 变化重新创建，旧的在下一次变化/卸载时统一 revoke
  const tempPreviewUrls = useMemo(
    () => tempFiles.map(f => URL.createObjectURL(f)),
    [tempFiles],
  )
  useEffect(() => {
    return () => {
      tempPreviewUrls.forEach(url => URL.revokeObjectURL(url))
    }
  }, [tempPreviewUrls])

  const removeTempFile = (index: number) => {
    setTempFiles(prev => prev.filter((_, i) => i !== index))
  }

  const autoHint = slot === 'tail_frame'
    ? '提示词留空时自动推理：分镜动作提示词 + 首帧 → 推导尾帧（两步 CoT）'
    : '提示词留空时自动推理：画面描述 + 本镜尾帧（如有）→ 反推首帧（两步 CoT）'

  const toggleRef = (id: string) => {
    setCheckedRefIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }

  const handleGenerate = async () => {
    setSubmitting(true)
    try {
      await api.createImageCandidate(project.id, shot.shot_id, {
        slot,
        customPrompt: prompt || undefined,
        refImageIds: [...checkedRefIds],
        files: tempFiles.length ? tempFiles : undefined,
      })
      setTempFiles([])
      onChanged()
    } finally {
      setSubmitting(false)
    }
  }

  const handleAdopt = async (c: ImageCandidate) => {
    await api.adoptImageCandidate(project.id, shot.shot_id, c.id)
    onChanged()
  }

  const handleDelete = async (c: ImageCandidate) => {
    await api.deleteImageCandidate(project.id, shot.shot_id, c.id)
    onChanged()
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>生成图片 — Shot #{shot.shot_id}</DialogTitle>
        </DialogHeader>

        {/* 目标槽位 */}
        <div className="space-y-2">
          <div className="text-sm font-medium text-zinc-700">
            目标槽位 <span className="text-xs font-normal text-zinc-400">（采纳的候选写入该槽位）</span>
          </div>
          <div className="inline-flex rounded-lg bg-zinc-100 p-0.5">
            {(['first_frame', 'tail_frame'] as const).map(s => (
              <button
                key={s}
                onClick={() => setSlot(s)}
                className={`rounded-md px-3.5 py-1.5 text-sm ${
                  slot === s ? 'bg-white font-semibold text-blue-600 shadow-sm' : 'text-zinc-500'
                }`}
              >
                {SLOT_LABEL[s]}
              </button>
            ))}
          </div>
        </div>

        {/* 自动推理提示 */}
        <div className="flex items-center gap-2 rounded-md bg-blue-50 px-3 py-2 text-xs text-blue-700">
          <Sparkles className="h-3.5 w-3.5 shrink-0" />
          {autoHint}
        </div>

        {/* 自定义提示词 */}
        <div className="space-y-2">
          <div className="text-sm font-medium text-zinc-700">
            自定义提示词 <span className="text-xs font-normal text-zinc-400">（可选 · 填写后覆盖自动推理）</span>
          </div>
          <Textarea
            value={prompt}
            onChange={e => setPrompt(e.target.value)}
            placeholder="例如：少女转身面向大海，手中饮品举至胸前，广角逆光，保持人物身份与服装不变…"
            rows={3}
          />
        </div>

        {/* 参考图勾选 + 临时上传 */}
        <div className="space-y-2">
          <div className="text-sm font-medium text-zinc-700">
            参考图 <span className="text-xs font-normal text-zinc-400">（默认自动带「角色」参考；临时上传仅本次生效）</span>
          </div>
          <div className="flex flex-wrap gap-2.5">
            {project.reference_images.map(r => (
              <button
                key={r.id}
                onClick={() => toggleRef(r.id)}
                className={`relative h-[72px] w-[72px] overflow-hidden rounded-md border-2 ${
                  checkedRefIds.has(r.id) ? 'border-blue-600' : 'border-zinc-200'
                }`}
                title={`${r.kind} 参考图`}
              >
                <img src={refMediaUrl(r.storage_path)} alt={`${r.kind} 参考图`} className="h-full w-full object-cover" />
                {checkedRefIds.has(r.id) && (
                  <span className="absolute left-1 top-1 rounded bg-blue-600 p-0.5">
                    <Check className="h-3 w-3 text-white" />
                  </span>
                )}
              </button>
            ))}
            {tempFiles.map((file, i) => (
              <div key={`${file.name}-${i}`} className="relative h-[72px] w-[72px] overflow-hidden rounded-md border-2 border-blue-600">
                <img src={tempPreviewUrls[i]} alt="临时参考图" className="h-full w-full object-cover" />
                <button
                  type="button"
                  aria-label="移除临时参考图"
                  onClick={() => removeTempFile(i)}
                  className="absolute right-1 top-1 flex h-4 w-4 items-center justify-center rounded-full bg-black/60 text-white"
                >
                  <X className="h-3 w-3" />
                </button>
              </div>
            ))}
            <button
              onClick={() => fileInput.current?.click()}
              className="flex h-[72px] w-[72px] flex-col items-center justify-center gap-1 rounded-md border border-dashed border-zinc-300 text-zinc-400"
            >
              <Plus className="h-4 w-4" />
              <span className="text-[11px]">临时上传</span>
            </button>
            <input
              ref={fileInput}
              type="file"
              accept="image/*"
              multiple
              className="hidden"
              onChange={e => {
                const newFiles = [...(e.target.files ?? [])]
                setTempFiles(prev => [...prev, ...newFiles])
                if (fileInput.current) fileInput.current.value = ''
              }}
            />
          </div>
        </div>

        {/* 候选画廊 */}
        <div className="space-y-2 border-t border-zinc-100 pt-3">
          <div className="flex items-center justify-between text-sm">
            <span className="font-medium text-zinc-700">
              候选画廊 · {SLOT_LABEL[slot]}
              <span className="ml-1 text-xs font-normal text-zinc-400">（每次生成 1 张，累积可对比）</span>
            </span>
            <span className="text-xs text-zinc-400">{candidates.length} 张</span>
          </div>
          <div className="flex flex-wrap gap-3">
            {candidates.map(c => (
              <div key={c.id} className="flex flex-col items-center gap-1.5">
                {c.status === 'generating' ? (
                  <div className="flex h-40 w-24 flex-col items-center justify-center gap-1.5 rounded-md border border-zinc-200 bg-zinc-50">
                    <Loader2 className="h-5 w-5 animate-spin text-blue-600" />
                    <span className="text-[11px] text-zinc-500">生成中…</span>
                  </div>
                ) : c.status === 'failed' ? (
                  <div
                    className="flex h-40 w-24 items-center justify-center rounded-md border border-red-200 bg-red-50 p-1 text-center text-[11px] text-red-600"
                    title={c.error ?? ''}
                  >
                    生成失败
                  </div>
                ) : (
                  <div className={`relative h-40 w-24 overflow-hidden rounded-md border-2 ${
                    c.adopted_at ? 'border-blue-600' : 'border-zinc-300'
                  }`}>
                    <img src={c.file_path ?? ''} alt="候选图" className="h-full w-full object-cover" />
                    {c.adopted_at && (
                      <span className="absolute left-1 top-1 flex items-center gap-0.5 rounded bg-blue-600 px-1.5 py-0.5 text-[10px] font-semibold text-white">
                        <Check className="h-2.5 w-2.5" /> 已采纳
                      </span>
                    )}
                  </div>
                )}
                <div className="flex gap-2 text-xs">
                  {c.status === 'done' && !c.adopted_at && (
                    <button className="font-semibold text-blue-600" onClick={() => handleAdopt(c)}>采纳</button>
                  )}
                  {c.status !== 'generating' && (
                    <button className="text-red-600" onClick={() => handleDelete(c)}>删除</button>
                  )}
                </div>
              </div>
            ))}
            {candidates.length === 0 && (
              <div className="text-xs text-zinc-400">暂无候选，点「生成」创建第一张</div>
            )}
          </div>
        </div>

        {/* 底部 */}
        <div className="flex items-center justify-between pt-1">
          <span className="text-xs text-zinc-400">生成走异步队列，可关闭弹窗稍后回来采纳</span>
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => onOpenChange(false)}>关闭</Button>
            <Button onClick={handleGenerate} disabled={submitting}>
              {submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
              生成 1 张候选
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
