// components/ShotCard.tsx - 分镜卡片组件

'use client'

import { useState, useRef, useEffect } from 'react'
import type { ComponentType } from 'react'
import { Edit, Link, Scissors, CheckSquare, Square, AlertTriangle, Play, Sparkles, Loader2, RefreshCw, X, ImagePlus, Mic, Undo2, User, ChevronDown, Crop, Upload } from 'lucide-react'
import { api } from '@/lib/api'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Switch } from '@/components/ui/switch'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { TrimDialog } from '@/components/TrimDialog'
import type { AspectRatio, Shot, ShotStatus } from '@/lib/types'

interface ShotCardProps {
  shot: Shot
  variant: 'script' | 'review' | 'generating'
  projectId?: string
  aspectRatio?: AspectRatio
  selected?: boolean
  prevLastFramePath?: string | null
  isReferenceVoice?: boolean
  hasReferenceVoice?: boolean
  autoVoiceCalibrate?: boolean
  onSelect?: (shotId: number) => void
  onEditScript?: (shotId: number, newText: string, newVisual: string, newAlign: boolean) => void
  onEditPrompt?: (shotId: number, prompt: string) => void
  onToggleAlign?: (shotId: number, align: boolean) => void
  onViewFirstFrame?: (shotId: number) => void
  onRedraw?: (shotId: number) => void
  onShotUpdated?: (shotId: number, updates: Partial<Shot>) => void
  onSetReferenceVoice?: (shotId: number) => void
  onVoiceConvert?: (shotId: number) => void
  onVoiceRevert?: (shotId: number) => void
  onCharacterCalibrate?: (shotId: number) => void
  onCharacterCalibrateRevert?: (shotId: number) => void
  onGenerateTailFrame?: (shotId: number) => void
  onDeleteTailFrame?: (shotId: number) => void
}

interface KeyframeMenuItem {
  icon: ComponentType<{ className?: string }>
  label: string
  disabled?: boolean
  onClick: () => void
}

/** 关键帧槽：缩略图 + hover 右上角 × 删除 + 「<label> ▾」分层菜单（单击即执行）。 */
function KeyframeSlot({
  label,
  accent,
  imgUrl,
  generating,
  failed,
  menuItems,
  onDelete,
  onPreview,
  onRetry,
}: {
  label: string
  accent: 'zinc' | 'indigo'
  imgUrl?: string | null
  generating?: boolean
  failed?: boolean
  menuItems: KeyframeMenuItem[]
  onDelete?: () => void
  onPreview?: (url: string) => void
  onRetry?: () => void
}) {
  const isIndigo = accent === 'indigo'
  // 路径已设置但文件 404（path-as-truth 前的过期数据）→ 视作空，显示占位而非裂图
  const [imgError, setImgError] = useState(false)
  useEffect(() => { setImgError(false) }, [imgUrl])
  return (
    <div className="flex flex-col items-center gap-1">
      <div className="relative group">
        <div
          className={`w-16 h-16 rounded overflow-hidden flex items-center justify-center bg-zinc-200 ${
            isIndigo ? 'border-2 border-indigo-400' : 'border border-zinc-200'
          }`}
        >
          {imgUrl && !imgError ? (
            <img
              src={imgUrl}
              alt={label}
              className="w-full h-full object-cover cursor-pointer hover:ring-2 hover:ring-offset-0 hover:ring-indigo-500"
              onClick={() => onPreview?.(imgUrl)}
              onError={() => setImgError(true)}
            />
          ) : (
            <ImagePlus className="w-5 h-5 text-zinc-400" />
          )}
          {generating && (
            <div className="absolute inset-0 bg-white/70 flex items-center justify-center">
              <Loader2 className="w-5 h-5 animate-spin text-indigo-600" />
            </div>
          )}
        </div>
        {imgUrl && onDelete && !generating && (
          <button
            type="button"
            title={`删除${label}`}
            onClick={onDelete}
            className="absolute -top-1.5 -right-1.5 w-[18px] h-[18px] rounded-full bg-red-500 text-white shadow opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center"
          >
            <X className="w-2.5 h-2.5" />
          </button>
        )}
      </div>
      {failed ? (
        <button
          type="button"
          onClick={onRetry}
          className="flex items-center gap-0.5 text-[11px] text-red-600 hover:text-red-700"
        >
          <RefreshCw className="w-3 h-3" />重试
        </button>
      ) : (
        <DropdownMenu>
          <DropdownMenuTrigger
            className={`flex items-center gap-1 px-2 py-0.5 rounded border text-xs bg-white hover:bg-zinc-50 ${
              isIndigo ? 'border-indigo-300 text-indigo-600' : 'border-zinc-300 text-zinc-700'
            }`}
          >
            {label}
            <ChevronDown className="w-3 h-3" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            {menuItems.map((it, i) => {
              const Icon = it.icon
              return (
                <DropdownMenuItem key={i} disabled={it.disabled} onClick={it.onClick}>
                  <Icon className="w-3.5 h-3.5 mr-2" />
                  {it.label}
                </DropdownMenuItem>
              )
            })}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  )
}

const shotTypeLabels: Record<string, string> = {
  'Close-up': '特写',
  'Medium Shot': '中景',
  'Wide Shot': '远景',
}

const shotStatusLabels: Record<ShotStatus, string> = {
  pending: '等待中',
  prompt_generating: '生成提示词',
  video_generating: '生成视频中',
  completed: '完成',
  failed: '失败',
}

const shotStatusColors: Record<ShotStatus, string> = {
  pending: 'bg-zinc-200 text-zinc-600',
  prompt_generating: 'bg-blue-100 text-blue-700',
  video_generating: 'bg-blue-100 text-blue-700',
  completed: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
}

export function ShotCard({
  shot,
  variant,
  projectId,
  aspectRatio,
  selected,
  prevLastFramePath,
  onSelect,
  onEditScript,
  onEditPrompt,
  onToggleAlign,
  onViewFirstFrame,
  onRedraw,
  onShotUpdated,
  isReferenceVoice,
  hasReferenceVoice,
  autoVoiceCalibrate,
  onSetReferenceVoice,
  onVoiceConvert,
  onVoiceRevert,
  onCharacterCalibrate,
  onCharacterCalibrateRevert,
  onGenerateTailFrame,
  onDeleteTailFrame,
}: ShotCardProps) {
  const [isEditDialogOpen, setIsEditDialogOpen] = useState(false)
  const [isPromptDialogOpen, setIsPromptDialogOpen] = useState(false)
  const [editText, setEditText] = useState(shot.text)
  const [editVisual, setEditVisual] = useState(shot.visual_description || '')
  const [editAlign, setEditAlign] = useState(shot.align_with_previous)
  const [editDuration, setEditDuration] = useState(shot.shot_duration)
  const [editPrompt, setEditPrompt] = useState(shot.motion_prompt || '')
  const [aiInstruction, setAiInstruction] = useState('')
  const [isAiLoading, setIsAiLoading] = useState(false)
  const [promptAiInstruction, setPromptAiInstruction] = useState('')
  const [isPromptAiLoading, setIsPromptAiLoading] = useState(false)
  const [isPromptRewriting, setIsPromptRewriting] = useState(false)
  const [isPlaying, setIsPlaying] = useState(false)
  const [isUploading, setIsUploading] = useState(false)
  const [dragIdx, setDragIdx] = useState<number | null>(null)
  const [dragOverIdx, setDragOverIdx] = useState<number | null>(null)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [isTrimOpen, setIsTrimOpen] = useState(false)
  const [videoVersion, setVideoVersion] = useState(0)
  const refUploadRef = useRef<HTMLInputElement>(null)
  const firstFrameInputRef = useRef<HTMLInputElement>(null)
  const tailFrameInputRef = useRef<HTMLInputElement>(null)
  const [aiError, setAiError] = useState('')

  // ── 关键帧管理（path-as-truth）：首帧 = custom_first_frame_path，尾帧 = target_last_frame_path ──
  const handleUploadFirstFrame = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file || !projectId) return
    try {
      const r = await api.uploadFirstFrame(projectId, shot.shot_id, file)
      onShotUpdated?.(shot.shot_id, { custom_first_frame_path: r.custom_first_frame_path })
    } catch { /* handled by parent */ } finally {
      if (firstFrameInputRef.current) firstFrameInputRef.current.value = ''
    }
  }

  const handleUploadTailFrame = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file || !projectId) return
    try {
      const r = await api.uploadTailFrame(projectId, shot.shot_id, file)
      onShotUpdated?.(shot.shot_id, { target_last_frame_path: r.target_last_frame_path, tf_status: 'done' })
    } catch { /* handled by parent */ } finally {
      if (tailFrameInputRef.current) tailFrameInputRef.current.value = ''
    }
  }

  const handleExtractFirstFrame = async () => {
    if (!projectId) return
    try {
      const r = await api.extractFirstFrame(projectId, shot.shot_id)
      onShotUpdated?.(shot.shot_id, { custom_first_frame_path: r.custom_first_frame_path })
    } catch { /* handled by parent */ }
  }

  const handleUsePrevLastFrame = async () => {
    if (!projectId) return
    try {
      const r = await api.usePrevLastFrame(projectId, shot.shot_id)
      onShotUpdated?.(shot.shot_id, { custom_first_frame_path: r.custom_first_frame_path })
    } catch { /* handled by parent */ }
  }

  const handleExtractLastFrame = async () => {
    if (!projectId) return
    try {
      const r = await api.extractLastFrame(projectId, shot.shot_id)
      onShotUpdated?.(shot.shot_id, { target_last_frame_path: r.target_last_frame_path, tf_status: 'done' })
    } catch { /* handled by parent */ }
  }

  const handleDeleteFirstFrame = async () => {
    if (!projectId) return
    try {
      await api.deleteFirstFrame(projectId, shot.shot_id)
      onShotUpdated?.(shot.shot_id, { custom_first_frame_path: null })
    } catch { /* handled by parent */ }
  }

  const handleSaveScript = () => {
    onEditScript?.(shot.shot_id, editText, editVisual, editAlign)
    setIsEditDialogOpen(false)
  }

  const handleSaveEdit = async () => {
    if (!projectId) return
    try {
      await api.patchShot(projectId, shot.shot_id, {
        text: editText,
        visual_description: editVisual,
        align_with_previous: editAlign,
        shot_duration: editDuration,
      })
      onShotUpdated?.(shot.shot_id, {
        text: editText,
        visual_description: editVisual,
        align_with_previous: editAlign,
        shot_duration: editDuration,
      })
      setIsEditDialogOpen(false)
    } catch {
      // error handled by parent
    }
  }

  const handleAiEdit = async () => {
    if (!projectId || !aiInstruction.trim()) return
    setIsAiLoading(true)
    setAiError('')
    try {
      const result = await api.aiEditShot(projectId, shot.shot_id, aiInstruction)
      setEditText(result.text)
      setEditVisual(result.visual_description)
      setAiInstruction('')
    } catch (e) {
      setAiError(e instanceof Error ? e.message : 'AI 生成失败')
    } finally {
      setIsAiLoading(false)
    }
  }

  const handleSavePrompt = () => {
    onEditPrompt?.(shot.shot_id, editPrompt)
    onShotUpdated?.(shot.shot_id, { motion_prompt: editPrompt })
    setIsPromptDialogOpen(false)
  }

  const handleAiEditPrompt = async () => {
    if (!projectId || !promptAiInstruction.trim()) return
    setIsPromptAiLoading(true)
    try {
      const result = await api.aiEditPrompt(projectId, shot.shot_id, promptAiInstruction)
      setEditPrompt(result.motion_prompt)
      setPromptAiInstruction('')
    } catch {
      // error handled by parent
    } finally {
      setIsPromptAiLoading(false)
    }
  }

  const handleRewritePrompt = async () => {
    if (!projectId) return
    setIsPromptRewriting(true)
    try {
      const result = await api.rewritePrompt(projectId, shot.shot_id)
      setEditPrompt(result.motion_prompt)
    } catch {
      // error handled by parent
    } finally {
      setIsPromptRewriting(false)
    }
  }

  const handleUploadRefs = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (!files?.length || !projectId) return
    setIsUploading(true)
    try {
      const result = await api.uploadShotReferences(projectId, shot.shot_id, Array.from(files))
      onShotUpdated?.(shot.shot_id, {
        custom_first_frame_path: result.custom_first_frame_path,
        custom_reference_paths: result.custom_reference_paths,
      })
    } catch {
      // error handled by parent
    } finally {
      setIsUploading(false)
      if (refUploadRef.current) refUploadRef.current.value = ''
    }
  }

  const handleDeleteRef = async (index: number) => {
    if (!projectId) return
    try {
      const result = await api.deleteShotReference(projectId, shot.shot_id, index)
      onShotUpdated?.(shot.shot_id, {
        custom_first_frame_path: result.custom_first_frame_path,
        custom_reference_paths: result.custom_reference_paths,
      })
    } catch {
      // error handled by parent
    }
  }

  const handleDrop = async (fromIdx: number, toIdx: number) => {
    if (fromIdx === toIdx || !projectId) return
    const order = customRefUrls.map((_, i) => i)
    const [moved] = order.splice(fromIdx, 1)
    order.splice(toIdx, 0, moved)
    try {
      const result = await api.reorderShotReferences(projectId, shot.shot_id, order)
      onShotUpdated?.(shot.shot_id, {
        custom_first_frame_path: result.custom_first_frame_path,
        custom_reference_paths: result.custom_reference_paths,
      })
    } catch { /* handled by parent */ }
  }

  // 当前断开分镜的参考图列表
  const customRefUrls: string[] = shot.custom_reference_paths
    ? shot.custom_reference_paths
    : shot.custom_first_frame_path
      ? [shot.custom_first_frame_path]
      : []

  // 首帧槽显示 custom_first_frame_path：由「用上一镜末帧」/「提取本镜首帧」/「上传首帧」
  // 或连续性初始化显式写入(不再依赖会失灵的 use_prev_last_frame live 链接)。
  const firstFrameUrl = shot.custom_first_frame_path

  // Edit dialog (shared between script and review variants)
  const editDialog = (
    <Dialog open={isEditDialogOpen} onOpenChange={setIsEditDialogOpen}>
      <DialogContent className="overflow-y-auto max-h-[90vh]">
        <DialogHeader>
          <DialogTitle>编辑分镜 #{shot.shot_id}</DialogTitle>
        </DialogHeader>
        {/* AI 建议区 */}
        {projectId && (
          <div className="mt-4 rounded-lg border border-zinc-200 bg-zinc-50 p-3 space-y-2">
            <div className="flex items-center gap-1 text-xs font-medium text-zinc-500">
              <Sparkles className="w-3 h-3" />AI 建议
            </div>
            <div className="flex gap-2">
              <Textarea
                value={aiInstruction}
                onChange={(e) => setAiInstruction(e.target.value)}
                rows={2}
                placeholder="告诉 AI 你想怎么改，例如：语气更轻松一点、把时长改成 4s..."
                className="text-sm"
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) handleAiEdit()
                }}
              />
              <Button
                size="sm"
                variant="outline"
                onClick={handleAiEdit}
                disabled={isAiLoading || !aiInstruction.trim()}
                className="shrink-0 self-end"
              >
                {isAiLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : '生成'}
              </Button>
            </div>
            {aiError && <p className="text-xs text-red-500">{aiError}</p>}
          </div>
        )}

        <div className="mt-3 space-y-3">
          <div>
            <label className="text-xs font-medium text-zinc-500 mb-1 block">台词</label>
            <Textarea
              value={editText}
              onChange={(e) => setEditText(e.target.value)}
              rows={3}
            />
          </div>
          <div>
            <label className="text-xs font-medium text-zinc-500 mb-1 block">视觉描述</label>
            <Textarea
              value={editVisual}
              onChange={(e) => setEditVisual(e.target.value)}
              rows={3}
              placeholder="描述镜头视觉内容..."
            />
          </div>
          <div className="flex items-center gap-4">
            <div className="flex items-center justify-between flex-1 rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2">
              <span className="text-sm font-medium text-zinc-700">
                {editAlign ? (
                  <><Link className="w-3.5 h-3.5 inline mr-1.5 text-blue-600" />与上一镜头连续</>
                ) : (
                  <><Scissors className="w-3.5 h-3.5 inline mr-1.5 text-zinc-400" />与上一镜头断开</>
                )}
              </span>
              <Switch
                checked={editAlign}
                onCheckedChange={(checked: boolean) => setEditAlign(checked)}
              />
            </div>
            <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2">
              <label className="text-xs font-medium text-zinc-500 block mb-1">时长</label>
              <select
                value={editDuration}
                onChange={(e) => setEditDuration(Number(e.target.value))}
                className="text-sm bg-transparent border-none outline-none"
              >
                <option value={4}>4s</option>
                <option value={6}>6s</option>
                <option value={8}>8s</option>
              </select>
            </div>
          </div>
        </div>
        <div className="flex justify-end gap-2 mt-4">
          <Button variant="outline" onClick={() => setIsEditDialogOpen(false)}>取消</Button>
          <Button onClick={variant === 'script' ? handleSaveScript : handleSaveEdit}>保存</Button>
        </div>
      </DialogContent>
    </Dialog>
  )

  const openEditDialog = () => {
    setEditText(shot.text)
    setEditVisual(shot.visual_description || '')
    setEditAlign(shot.align_with_previous)
    setEditDuration(shot.shot_duration)
    setAiInstruction('')
    setAiError('')
    setIsEditDialogOpen(true)
  }

  // Script 变体 - 脚本审批页
  if (variant === 'script') {
    return (
      <>
        <Card data-testid={`shot-card-${shot.shot_id}`} className={`relative ${shot.word_count_warning ? 'border-yellow-400' : ''}`}>
          <CardContent className="p-4 space-y-3">
            <div className="flex items-start justify-between">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-zinc-500">#{shot.shot_id}</span>
                <Badge variant="secondary">{shotTypeLabels[shot.shot_type]}</Badge>
                <span className="text-sm text-zinc-400">{shot.shot_duration}s</span>
              </div>
              <Button variant="ghost" size="sm" onClick={openEditDialog}>
                <Edit className="w-4 h-4" />
              </Button>
            </div>

            <p className="text-sm text-zinc-700 leading-relaxed">{shot.text}</p>

            {shot.visual_description && (
              <p className="text-xs text-zinc-400 italic leading-relaxed">{shot.visual_description}</p>
            )}

            <div className="flex items-center justify-between pt-2 border-t">
              <div className="flex items-center gap-2">
                {shot.word_count_warning && (
                  <Badge variant="outline" className="border-yellow-400 text-yellow-600">
                    <AlertTriangle className="w-3 h-3 mr-1" />字数警告
                  </Badge>
                )}
              </div>

              <div className="flex items-center gap-2">
                <span className="text-xs text-zinc-400">
                  {shot.align_with_previous ? (
                    <><Link className="w-3 h-3 inline mr-1" />连续</>
                  ) : (
                    <><Scissors className="w-3 h-3 inline mr-1" />断开</>
                  )}
                </span>
                <Switch
                  checked={shot.align_with_previous}
                  onCheckedChange={(checked: boolean) => onToggleAlign?.(shot.shot_id, checked)}
                />
              </div>
            </div>

            {/* 参考图上传 */}
            {!shot.align_with_previous && (
              <div className="space-y-2 pt-2 border-t">
                <div className="flex items-center gap-2">
                  <input ref={refUploadRef} type="file" accept="image/*" multiple className="hidden" onChange={handleUploadRefs} />
                  <Button variant="outline" size="sm" onClick={() => refUploadRef.current?.click()} disabled={isUploading}>
                    {isUploading ? <Loader2 className="w-3 h-3 animate-spin mr-1" /> : <ImagePlus className="w-3 h-3 mr-1" />}
                    添加参考图
                  </Button>
                </div>
                {customRefUrls.length > 0 && (
                  <div className="flex flex-wrap gap-2">
                    {customRefUrls.map((url, idx) => (
                      <div
                        key={idx}
                        draggable
                        onDragStart={() => setDragIdx(idx)}
                        onDragOver={(e) => { e.preventDefault(); setDragOverIdx(idx) }}
                        onDrop={() => { if (dragIdx !== null) handleDrop(dragIdx, idx); setDragIdx(null); setDragOverIdx(null) }}
                        onDragEnd={() => { setDragIdx(null); setDragOverIdx(null) }}
                        className={`relative group cursor-grab ${dragOverIdx === idx ? 'ring-2 ring-indigo-400 rounded' : ''}`}
                      >
                        <img src={url} alt={`参考图 ${idx + 1}`} className="w-14 h-14 object-cover rounded border cursor-pointer" onClick={() => setPreviewUrl(url)} />
                        <button
                          onClick={() => handleDeleteRef(idx)}
                          className="absolute -top-1.5 -right-1.5 w-4 h-4 bg-red-500 text-white rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
                        >
                          <X className="w-2.5 h-2.5" />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </CardContent>
        </Card>

        {editDialog}
      </>
    )
  }

  // Generating 变体 - 生成中状态
  if (variant === 'generating') {
    return (
      <Card>
        <CardContent className="p-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className="text-sm font-medium">#{shot.shot_id}</span>
            <Badge className={shotStatusColors[shot.status]}>
              {shotStatusLabels[shot.status]}
            </Badge>
          </div>

          <div className="flex items-center gap-2">
            {(shot.status === 'prompt_generating' || shot.status === 'video_generating') && (
              <>
                <div className="w-5 h-5 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
                {projectId && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-7 px-2 text-xs text-red-500 hover:text-red-700"
                    onClick={async () => {
                      try {
                        await api.cancelGeneration(projectId)
                      } catch { /* handled by parent */ }
                    }}
                  >
                    <X className="w-3 h-3 mr-1" />取消
                  </Button>
                )}
              </>
            )}

            {shot.status === 'completed' && (
              <Badge variant="outline" className="border-green-500 text-green-600">
                <CheckSquare className="w-3 h-3 mr-1" />完成
              </Badge>
            )}

            {shot.status === 'failed' && (
              <Badge variant="outline" className="border-red-500 text-red-600">
                <AlertTriangle className="w-3 h-3 mr-1" />失败
              </Badge>
            )}
          </div>
        </CardContent>
      </Card>
    )
  }

  // Review 变体 - 分镜审批页（统一编辑）
  return (
    <>
      <Card data-testid={`shot-card-${shot.shot_id}`} className={`relative ${selected ? 'ring-2 ring-blue-500' : ''} ${shot.word_count_warning ? 'border-yellow-400' : ''}`}>
        <CardContent className="p-4 space-y-3">
          <div className="flex items-start justify-between">
            <div className="flex items-center gap-2">
              {onSelect && (
                <button
                  data-testid={`shot-select-${shot.shot_id}`}
                  onClick={() => onSelect(shot.shot_id)}
                  className="text-zinc-400 hover:text-blue-600"
                >
                  {selected ? (
                    <CheckSquare className="w-5 h-5 text-blue-600" />
                  ) : (
                    <Square className="w-5 h-5" />
                  )}
                </button>
              )}
              <span className="text-sm font-medium">#{shot.shot_id}</span>
              <Badge variant="secondary">{shotTypeLabels[shot.shot_type]}</Badge>
              <span className="text-sm text-zinc-400">{shot.shot_duration}s</span>
              {isReferenceVoice && (
                <Badge className="bg-amber-100 text-amber-700 border-amber-300">
                  <Mic className="w-3 h-3 mr-1" />基准音色
                </Badge>
              )}
              {shot.word_count_warning && (
                <Badge variant="outline" className="border-yellow-400 text-yellow-600">
                  <AlertTriangle className="w-3 h-3 mr-1" />字数
                </Badge>
              )}
            </div>

            <div className="flex items-center gap-1">
              {shot.align_with_previous ? (
                <Badge variant="outline" className="text-xs">
                  <Link className="w-3 h-3 mr-1" />连续
                </Badge>
              ) : (
                <Badge variant="outline" className="text-xs border-zinc-300">
                  <Scissors className="w-3 h-3 mr-1" />断开
                </Badge>
              )}
              <Button variant="ghost" size="sm" onClick={openEditDialog}>
                <Edit className="w-4 h-4" />
              </Button>
            </div>
          </div>

          {/* 视频播放器 */}
          {shot.video_path && isPlaying && (
            <div className="relative rounded-lg overflow-hidden">
              <video
                src={shot.video_path}
                controls
                autoPlay
                className="w-full"
              />
            </div>
          )}

          {shot.video_path && !isPlaying && (
            <div
              className="relative rounded-lg overflow-hidden cursor-pointer group"
              onClick={() => setIsPlaying(true)}
            >
              <img
                src={shot.last_frame_path || shot.first_frame_path || undefined}
                alt={`Shot ${shot.shot_id}`}
                className="w-full"
              />
              <div className="absolute inset-0 flex items-center justify-center bg-black/30 group-hover:bg-black/20 transition-colors">
                <Play className="w-12 h-12 text-white opacity-80" />
              </div>
            </div>
          )}

          {/* 失败信息 */}
          {shot.status === 'failed' && shot.error_message && (
            <div className="text-sm text-red-600 bg-red-50 p-3 rounded flex items-start gap-2">
              <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
              <span>{shot.error_message}</span>
            </div>
          )}

          {/* 台词 */}
          <div className="text-sm text-zinc-600 bg-zinc-50 p-3 rounded">
            {shot.text}
          </div>

          {/* 视觉描述 */}
          {shot.visual_description && (
            <p className="text-xs text-zinc-400 italic leading-relaxed">{shot.visual_description}</p>
          )}

          {/* 运镜提示词 */}
          {shot.motion_prompt && (
            <div className="text-xs text-zinc-400 bg-zinc-50 p-3 rounded border-l-2 border-zinc-200 leading-relaxed">
              <span className="font-medium text-zinc-500 block mb-1">运镜提示词</span>
              {shot.motion_prompt}
            </div>
          )}

          {/* 参考图提示 + 上传（所有镜头均可上传） */}
          <div className="space-y-2">
            {!shot.align_with_previous && shot.reference_image_hint && (
              <div className="text-xs text-amber-700 bg-amber-50 p-2 rounded">
                {shot.reference_image_hint}
              </div>
            )}
            {/* 参考图上传 */}
            <div className="flex items-center gap-2">
              <input ref={refUploadRef} type="file" accept="image/*" multiple className="hidden" onChange={handleUploadRefs} />
              <Button variant="outline" size="sm" onClick={() => refUploadRef.current?.click()} disabled={isUploading}>
                {isUploading ? <Loader2 className="w-3 h-3 animate-spin mr-1" /> : <ImagePlus className="w-3 h-3 mr-1" />}
                添加参考图
              </Button>
            </div>
            {customRefUrls.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {customRefUrls.map((url, idx) => (
                  <div
                    key={idx}
                    draggable
                    onDragStart={() => setDragIdx(idx)}
                    onDragOver={(e) => { e.preventDefault(); setDragOverIdx(idx) }}
                    onDrop={() => { if (dragIdx !== null) handleDrop(dragIdx, idx); setDragIdx(null); setDragOverIdx(null) }}
                    onDragEnd={() => { setDragIdx(null); setDragOverIdx(null) }}
                    className={`relative group cursor-grab ${dragOverIdx === idx ? 'ring-2 ring-indigo-400 rounded' : ''}`}
                  >
                    <img src={url} alt={`参考图 ${idx + 1}`} className="w-14 h-14 object-cover rounded border cursor-pointer" onClick={() => setPreviewUrl(url)} />
                    <button
                      onClick={() => handleDeleteRef(idx)}
                      className="absolute -top-1.5 -right-1.5 w-4 h-4 bg-red-500 text-white rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
                    >
                      <X className="w-2.5 h-2.5" />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* 自动裁剪开关 */}
          <div className="flex items-center justify-between rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2">
            <span className="text-sm text-zinc-600">
              <Scissors className="w-3.5 h-3.5 inline mr-1.5 text-zinc-400" />
              生成后自动裁剪（尾帧对齐, 推荐打开）
            </span>
            <Switch
              checked={shot.auto_trim}
              onCheckedChange={async (checked: boolean) => {
                if (!projectId) return
                try {
                  await api.patchShot(projectId, shot.shot_id, { auto_trim: checked })
                  onShotUpdated?.(shot.shot_id, { auto_trim: checked })
                } catch { /* handled by parent */ }
              }}
            />
          </div>

          {/* 尾帧 generating/failed 状态已并入下方「关键帧管理」控件 */}

          {/* 音色状态指示器 */}
          {shot.vc_status === 'converting' && (
            <div className="flex items-center gap-2 text-sm text-blue-600 bg-blue-50 p-2 rounded">
              <Loader2 className="w-4 h-4 animate-spin" />
              正在转换音色...
              {autoVoiceCalibrate && (
                <span className="rounded bg-neutral-700 px-1 text-[10px] text-neutral-300">自动</span>
              )}
            </div>
          )}
          {shot.vc_status === 'done' && (
            <div className="flex items-center justify-between text-sm text-green-700 bg-green-50 p-2 rounded">
              <span>音色已统一</span>
              {onVoiceRevert && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-xs text-green-700 hover:text-red-600"
                  onClick={() => onVoiceRevert(shot.shot_id)}
                >
                  <Undo2 className="w-3 h-3 mr-1" />还原
                </Button>
              )}
            </div>
          )}
          {shot.vc_status === 'failed' && (
            <div className="text-sm text-red-600 bg-red-50 p-2 rounded space-y-1">
              <div className="flex items-center gap-1">
                <AlertTriangle className="w-3 h-3" />
                音色转换失败
              </div>
              {shot.vc_error_message && (
                <p className="text-xs text-red-500">{shot.vc_error_message}</p>
              )}
              {onVoiceConvert && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-xs"
                  onClick={() => onVoiceConvert(shot.shot_id)}
                >
                  <RefreshCw className="w-3 h-3 mr-1" />重试
                </Button>
              )}
            </div>
          )}

          {/* 人物校准状态指示器 */}
          {shot.cc_status === 'calibrating' && (
            <div className="flex items-center gap-2 text-sm text-purple-600 bg-purple-50 p-2 rounded">
              <Loader2 className="w-4 h-4 animate-spin" />
              正在校准人物...
            </div>
          )}
          {shot.cc_status === 'done' && (
            <div className="flex items-center justify-between text-sm text-purple-700 bg-purple-50 p-2 rounded">
              <div className="flex items-center gap-2">
                {shot.last_frame_path && (
                  <img
                    src={shot.last_frame_path}
                    alt="校准后尾帧"
                    className="w-12 h-12 object-cover rounded cursor-pointer border-2 border-purple-300 hover:ring-2 hover:ring-purple-500"
                    onClick={() => setPreviewUrl(shot.last_frame_path)}
                  />
                )}
                <span>人物已校准</span>
              </div>
              {onCharacterCalibrateRevert && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-xs text-purple-700 hover:text-red-600"
                  onClick={() => onCharacterCalibrateRevert(shot.shot_id)}
                >
                  <Undo2 className="w-3 h-3 mr-1" />还原
                </Button>
              )}
            </div>
          )}
          {shot.cc_status === 'failed' && (
            <div className="text-sm text-red-600 bg-red-50 p-2 rounded space-y-1">
              <div className="flex items-center gap-1">
                <AlertTriangle className="w-3 h-3" />
                人物校准失败
              </div>
              {shot.cc_error_message && (
                <p className="text-xs text-red-500">{shot.cc_error_message}</p>
              )}
              {onCharacterCalibrate && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-xs"
                  onClick={() => onCharacterCalibrate(shot.shot_id)}
                >
                  <RefreshCw className="w-3 h-3 mr-1" />重试
                </Button>
              )}
            </div>
          )}

          {/* 关键帧管理：首帧（custom_first_frame_path）/ 尾帧（target_last_frame_path）*/}
          {projectId && (
            <div className="flex items-start gap-4 p-3 bg-zinc-50 rounded border">
              <input ref={firstFrameInputRef} type="file" accept="image/*" className="hidden" onChange={handleUploadFirstFrame} />
              <input ref={tailFrameInputRef} type="file" accept="image/*" className="hidden" onChange={handleUploadTailFrame} />
              <KeyframeSlot
                label="首帧"
                accent="zinc"
                imgUrl={firstFrameUrl}
                onPreview={setPreviewUrl}
                onDelete={handleDeleteFirstFrame}
                menuItems={[
                  { icon: Link, label: '用上一镜末帧', disabled: !prevLastFramePath, onClick: handleUsePrevLastFrame },
                  { icon: Crop, label: '提取本镜首帧', disabled: !shot.first_frame_path, onClick: handleExtractFirstFrame },
                  { icon: Upload, label: '上传首帧', onClick: () => firstFrameInputRef.current?.click() },
                ]}
              />
              <KeyframeSlot
                label="尾帧"
                accent="indigo"
                imgUrl={shot.target_last_frame_path}
                generating={shot.tf_status === 'generating'}
                failed={shot.tf_status === 'failed'}
                onPreview={setPreviewUrl}
                onDelete={onDeleteTailFrame ? () => onDeleteTailFrame(shot.shot_id) : undefined}
                onRetry={onGenerateTailFrame ? () => onGenerateTailFrame(shot.shot_id) : undefined}
                menuItems={[
                  ...(onGenerateTailFrame
                    ? [{ icon: Sparkles, label: '生成尾帧', disabled: !shot.motion_prompt, onClick: () => onGenerateTailFrame(shot.shot_id) }]
                    : []),
                  { icon: Crop, label: '提取本镜尾帧', disabled: !shot.last_frame_path, onClick: handleExtractLastFrame },
                  { icon: Upload, label: '上传尾帧', onClick: () => tailFrameInputRef.current?.click() },
                ]}
              />
              {shot.tf_status === 'failed' && shot.tf_error_message && (
                <span className="text-[11px] text-red-500 mt-1">{shot.tf_error_message}</span>
              )}
            </div>
          )}

          {/* 操作栏 */}
          <div className="flex flex-wrap items-center gap-1 pt-2">
              {shot.video_path && projectId && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setIsTrimOpen(true)}
                >
                  <Scissors className="w-4 h-4 mr-1" />裁剪
                </Button>
              )}
              {shot.motion_prompt != null ? (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => { setEditPrompt(shot.motion_prompt || ''); setIsPromptDialogOpen(true) }}
                >
                  <Edit className="w-4 h-4 mr-1" />运镜提示词
                </Button>
              ) : projectId && (
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={isPromptRewriting}
                  onClick={async () => {
                    setIsPromptRewriting(true)
                    try {
                      const result = await api.rewritePrompt(projectId, shot.shot_id)
                      onShotUpdated?.(shot.shot_id, { motion_prompt: result.motion_prompt })
                      setEditPrompt(result.motion_prompt)
                    } catch { /* handled by parent */ } finally {
                      setIsPromptRewriting(false)
                    }
                  }}
                >
                  {isPromptRewriting ? <Loader2 className="w-4 h-4 mr-1 animate-spin" /> : <Sparkles className="w-4 h-4 mr-1" />}
                  生成运镜提示词
                </Button>
              )}
              {/* 音色操作按钮 */}
              {shot.video_path && onSetReferenceVoice && (
                <Button
                  variant={isReferenceVoice ? 'secondary' : 'ghost'}
                  size="sm"
                  onClick={() => onSetReferenceVoice(shot.shot_id)}
                  className={isReferenceVoice ? 'bg-amber-100 hover:bg-amber-200 text-amber-700' : ''}
                >
                  <Mic className="w-4 h-4 mr-1" />
                  {isReferenceVoice ? '取消基准' : '设为基准'}
                </Button>
              )}
              {shot.video_path && !isReferenceVoice && hasReferenceVoice && !shot.vc_status && onVoiceConvert && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => onVoiceConvert(shot.shot_id)}
                >
                  <Mic className="w-4 h-4 mr-1" />转换音色
                </Button>
              )}
              {shot.video_path && onCharacterCalibrate && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => onCharacterCalibrate(shot.shot_id)}
                  disabled={shot.cc_status === 'calibrating'}
                >
                  <User className="w-4 h-4 mr-1" />{shot.cc_status === 'done' ? '重新校准' : '校准人物'}
                </Button>
              )}
              {(shot.status === 'completed' || shot.status === 'failed' || shot.status === 'pending') && onRedraw && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => onRedraw(shot.shot_id)}
                >
                  <RefreshCw className="w-4 h-4 mr-1" />生成分镜
                </Button>
              )}
          </div>
        </CardContent>
      </Card>

      {editDialog}

      <Dialog open={isPromptDialogOpen} onOpenChange={setIsPromptDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>编辑运镜提示词 #{shot.shot_id}</DialogTitle>
          </DialogHeader>
          <Textarea
            value={editPrompt}
            onChange={(e) => setEditPrompt(e.target.value)}
            rows={4}
            className="mt-4"
            placeholder="描述镜头运动方式..."
          />
          {projectId && (
            <div className="flex gap-2 mt-2">
              <Textarea
                value={promptAiInstruction}
                onChange={(e) => setPromptAiInstruction(e.target.value)}
                rows={1}
                placeholder="AI 修改指令，例如：加入缓慢推进的镜头运动"
                className="text-sm"
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) handleAiEditPrompt()
                }}
              />
              <Button
                size="sm"
                variant="outline"
                onClick={handleAiEditPrompt}
                disabled={isPromptAiLoading || !promptAiInstruction.trim()}
                className="shrink-0 self-end"
              >
                {isPromptAiLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Sparkles className="w-3 h-3 mr-1" />AI</>}
              </Button>
            </div>
          )}
          <div className="flex justify-between mt-4">
            {projectId && (
              <Button
                variant="outline"
                onClick={handleRewritePrompt}
                disabled={isPromptRewriting}
              >
                {isPromptRewriting ? <Loader2 className="w-4 h-4 mr-1 animate-spin" /> : <RefreshCw className="w-4 h-4 mr-1" />}
                重写
              </Button>
            )}
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => setIsPromptDialogOpen(false)}>取消</Button>
              <Button onClick={handleSavePrompt}>保存</Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
      {/* 裁剪弹窗 */}
      {projectId && (
        <TrimDialog
          shot={{
            ...shot,
            video_path: videoVersion ? `${shot.video_path}?v=${videoVersion}` : shot.video_path,
          }}
          projectId={projectId}
          aspectRatio={aspectRatio}
          open={isTrimOpen}
          onOpenChange={setIsTrimOpen}
          onTrimmed={({ video_path, last_frame_path, version }) => {
            setVideoVersion(version)
            setIsPlaying(false)
            onShotUpdated?.(shot.shot_id, {
              video_path: `${video_path}?v=${version}`,
              last_frame_path: `${last_frame_path}?v=${version}`,
            })
          }}
        />
      )}

      {/* 参考图预览 */}
      {previewUrl && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70" onClick={() => setPreviewUrl(null)}>
          <button
            className="absolute top-4 right-4 w-8 h-8 bg-white rounded-full flex items-center justify-center shadow"
            onClick={() => setPreviewUrl(null)}
          >
            <X className="w-5 h-5 text-zinc-700" />
          </button>
          <img
            src={previewUrl}
            alt="预览"
            className="max-w-[90vw] max-h-[90vh] object-contain rounded-lg shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )}
    </>
  )
}
