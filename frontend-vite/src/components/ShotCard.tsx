// components/ShotCard.tsx - 分镜卡片组件

'use client'

import { useState, useRef } from 'react'
import { Edit, Link, Scissors, CheckSquare, Square, AlertTriangle, Play, Sparkles, Loader2, RefreshCw, X, ImagePlus, Mic, Undo2 } from 'lucide-react'
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
import { TrimDialog } from '@/components/TrimDialog'
import type { Shot, ShotStatus } from '@/lib/types'

interface ShotCardProps {
  shot: Shot
  variant: 'script' | 'review' | 'generating'
  projectId?: string
  selected?: boolean
  prevLastFramePath?: string | null
  isReferenceVoice?: boolean
  hasReferenceVoice?: boolean
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
  onSetReferenceVoice,
  onVoiceConvert,
  onVoiceRevert,
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
  const [isPlaying, setIsPlaying] = useState(false)
  const [isUploading, setIsUploading] = useState(false)
  const [dragIdx, setDragIdx] = useState<number | null>(null)
  const [dragOverIdx, setDragOverIdx] = useState<number | null>(null)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [isTrimOpen, setIsTrimOpen] = useState(false)
  const [videoVersion, setVideoVersion] = useState(0)
  const refUploadRef = useRef<HTMLInputElement>(null)
  const [aiError, setAiError] = useState('')

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
    setIsPromptDialogOpen(false)
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

            {/* 断开分镜参考图提示 + 上传 */}
            {!shot.align_with_previous && (
              <div className="space-y-2 pt-2 border-t">
                {shot.reference_image_hint && (
                  <div className="text-xs text-amber-700 bg-amber-50 p-2 rounded">
                    {shot.reference_image_hint}
                  </div>
                )}
                <div className="flex items-center gap-2 flex-wrap">
                  {customRefUrls.map((url, i) => (
                    <div key={i} className="relative group">
                      <img
                        src={url}
                        alt={`参考图 ${i + 1}`}
                        className="w-10 h-10 object-cover rounded border"
                      />
                      <button
                        className="absolute -top-1 -right-1 w-4 h-4 bg-red-500 text-white rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
                        onClick={() => handleDeleteRef(i)}
                      >
                        <X className="w-3 h-3" />
                      </button>
                    </div>
                  ))}
                  <input
                    ref={refUploadRef}
                    type="file"
                    accept="image/*"
                    multiple
                    className="hidden"
                    onChange={handleUploadRefs}
                  />
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={isUploading}
                    onClick={() => refUploadRef.current?.click()}
                  >
                    {isUploading ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                      <><ImagePlus className="w-4 h-4 mr-1" />参考图</>
                    )}
                  </Button>
                </div>
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

          {(shot.status === 'prompt_generating' || shot.status === 'video_generating') && (
            <div className="w-5 h-5 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
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
            <div className="relative aspect-video bg-zinc-900 rounded-lg overflow-hidden">
              <video
                src={shot.video_path}
                controls
                autoPlay
                className="w-full h-full"
              />
            </div>
          )}

          {shot.video_path && !isPlaying && (
            <div
              className="relative aspect-video bg-zinc-900 rounded-lg overflow-hidden flex items-center justify-center cursor-pointer group"
              onClick={() => setIsPlaying(true)}
            >
              <img
                src={shot.last_frame_path || shot.first_frame_path || undefined}
                alt={`Shot ${shot.shot_id}`}
                className="w-full h-full object-cover"
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

          {/* 断开分镜参考图提示 + 上传 */}
          {!shot.align_with_previous && (
            <div className="space-y-2">
              {shot.reference_image_hint && (
                <div className="text-xs text-amber-700 bg-amber-50 p-2 rounded">
                  {shot.reference_image_hint}
                </div>
              )}
              {prevLastFramePath && (
                <label className="flex items-center gap-2 text-xs text-zinc-600 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={shot.use_prev_last_frame}
                    onChange={async (e) => {
                      if (!projectId) return
                      const val = e.target.checked
                      try {
                        await api.patchShot(projectId, shot.shot_id, { use_prev_last_frame: val })
                        onShotUpdated?.(shot.shot_id, { use_prev_last_frame: val })
                      } catch { /* handled by parent */ }
                    }}
                    className="rounded border-zinc-300"
                  />
                  使用上一镜头末帧作为首张参考图
                  {shot.use_prev_last_frame && (
                    <img src={prevLastFramePath} alt="上一镜头末帧" className="w-12 h-12 object-cover rounded border ml-1 cursor-pointer" onClick={() => setPreviewUrl(prevLastFramePath!)} />
                  )}
                </label>
              )}
              <div className="flex items-center gap-2 flex-wrap">
                {customRefUrls.map((url, i) => (
                  <div
                    key={i}
                    className={`relative group cursor-grab ${dragOverIdx === i ? 'ring-2 ring-blue-400' : ''}`}
                    draggable
                    onDragStart={() => setDragIdx(i)}
                    onDragOver={(e) => { e.preventDefault(); setDragOverIdx(i) }}
                    onDragLeave={() => setDragOverIdx(null)}
                    onDrop={(e) => {
                      e.preventDefault()
                      setDragOverIdx(null)
                      if (dragIdx !== null) handleDrop(dragIdx, i)
                      setDragIdx(null)
                    }}
                    onDragEnd={() => { setDragIdx(null); setDragOverIdx(null) }}
                  >
                    <img
                      src={url}
                      alt={`参考图 ${i + 1}`}
                      className={`w-12 h-12 object-cover rounded border ${dragIdx === i ? 'opacity-40' : ''}`}
                      onClick={() => setPreviewUrl(url)}
                    />
                    <span className="absolute bottom-0 left-0 bg-black/60 text-white text-[10px] px-1 rounded-tr">{i + 1}</span>
                    <button
                      className="absolute -top-1 -right-1 w-4 h-4 bg-red-500 text-white rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
                      onClick={() => handleDeleteRef(i)}
                    >
                      <X className="w-3 h-3" />
                    </button>
                  </div>
                ))}
                <input
                  ref={refUploadRef}
                  type="file"
                  accept="image/*"
                  multiple
                  className="hidden"
                  onChange={handleUploadRefs}
                />
                <Button
                  variant="outline"
                  size="sm"
                  className="h-12 px-3"
                  disabled={isUploading}
                  onClick={() => refUploadRef.current?.click()}
                >
                  {isUploading ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    <><ImagePlus className="w-4 h-4 mr-1" />参考图</>
                  )}
                </Button>
              </div>
            </div>
          )}

          {/* 音色状态指示器 */}
          {shot.vc_status === 'converting' && (
            <div className="flex items-center gap-2 text-sm text-blue-600 bg-blue-50 p-2 rounded">
              <Loader2 className="w-4 h-4 animate-spin" />
              正在转换音色...
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

          {/* 操作栏 */}
          <div className="flex items-center justify-between pt-2">
            <div className="flex items-center gap-2">
              {shot.last_frame_path && (
                <img
                  src={shot.last_frame_path}
                  alt="尾帧"
                  className="w-10 h-10 object-cover rounded cursor-pointer hover:ring-2 hover:ring-blue-500"
                  onClick={() => setPreviewUrl(shot.last_frame_path)}
                />
              )}
            </div>

            <div className="flex items-center gap-1">
              {(shot.status === 'completed' || shot.status === 'failed') && onRedraw && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => onRedraw(shot.shot_id)}
                >
                  <RefreshCw className="w-4 h-4 mr-1" />重新生成
                </Button>
              )}
              {shot.status === 'completed' && shot.video_path && projectId && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setIsTrimOpen(true)}
                >
                  <Scissors className="w-4 h-4 mr-1" />裁剪
                </Button>
              )}
              {shot.motion_prompt != null && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setIsPromptDialogOpen(true)}
                >
                  <Edit className="w-4 h-4 mr-1" />运镜提示词
                </Button>
              )}
              {/* 音色操作按钮 */}
              {shot.status === 'completed' && onSetReferenceVoice && (
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
              {shot.status === 'completed' && !isReferenceVoice && hasReferenceVoice && !shot.vc_status && onVoiceConvert && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => onVoiceConvert(shot.shot_id)}
                >
                  <Mic className="w-4 h-4 mr-1" />转换音色
                </Button>
              )}
            </div>
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
          <div className="flex justify-end gap-2 mt-4">
            <Button variant="outline" onClick={() => setIsPromptDialogOpen(false)}>取消</Button>
            <Button onClick={handleSavePrompt}>保存</Button>
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
