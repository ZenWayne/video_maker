import { useEffect, useRef, useState, useCallback } from 'react'
import { Loader2, ChevronLeft, ChevronRight, Play, Square, Undo2, Crosshair } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { api } from '@/lib/api'
import type { AspectRatio, Shot } from '@/lib/types'

interface TrimDialogProps {
  shot: Shot
  projectId: string
  aspectRatio?: AspectRatio
  open: boolean
  onOpenChange: (open: boolean) => void
  onTrimmed: (updates: {
    video_path: string
    last_frame_path: string
    version: number
  }) => void
}

export function TrimDialog({
  shot,
  projectId,
  aspectRatio,
  open,
  onOpenChange,
  onTrimmed,
}: TrimDialogProps) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [fps, setFps] = useState(24)
  const [totalFrames, setTotalFrames] = useState(0)
  const [duration, setDuration] = useState(0)
  const [endFrame, setEndFrame] = useState(0)
  const [isLoading, setIsLoading] = useState(true)
  const [isTrimming, setIsTrimming] = useState(false)
  const [isRestoring, setIsRestoring] = useState(false)
  const [isAligning, setIsAligning] = useState(false)
  const [isPreviewing, setIsPreviewing] = useState(false)
  const [hasBackup, setHasBackup] = useState(false)
  const [error, setError] = useState('')
  const minFrames = 24

  const rvfcRef = useRef<number>(0)
  const endFrameRef = useRef(0)

  const stopPreview = useCallback(() => {
    const v = videoRef.current
    if (rvfcRef.current) {
      if (v && 'cancelVideoFrameCallback' in v) {
        ;(v as any).cancelVideoFrameCallback(rvfcRef.current)
      } else {
        cancelAnimationFrame(rvfcRef.current)
      }
      rvfcRef.current = 0
    }
    v?.pause()
    setIsPreviewing(false)
  }, [])

  const handlePreview = useCallback(() => {
    if (isPreviewing) {
      stopPreview()
      return
    }
    const v = videoRef.current
    if (!v || fps <= 0) return
    endFrameRef.current = endFrame
    v.currentTime = 0
    v.play()
    setIsPreviewing(true)

    let framesShown = 0
    const onFrame = () => {
      framesShown++
      if (!videoRef.current || framesShown >= endFrameRef.current) {
        videoRef.current?.pause()
        setIsPreviewing(false)
        return
      }
      rvfcRef.current = (videoRef.current as any).requestVideoFrameCallback(onFrame)
    }

    if ('requestVideoFrameCallback' in v) {
      rvfcRef.current = (v as any).requestVideoFrameCallback(onFrame)
    } else {
      // Fallback: use time-based check with half-frame offset to avoid overshoot
      const endSec = (endFrame - 0.5) / fps
      const checkEnd = () => {
        if (!videoRef.current) return
        if (videoRef.current.paused) return
        if (videoRef.current.currentTime >= endSec) {
          videoRef.current.pause()
          setIsPreviewing(false)
          return
        }
        rvfcRef.current = requestAnimationFrame(checkEnd)
      }
      rvfcRef.current = requestAnimationFrame(checkEnd)
    }
  }, [isPreviewing, stopPreview, endFrame, fps])

  useEffect(() => {
    return () => {
      const v = videoRef.current
      if (rvfcRef.current) {
        if (v && 'cancelVideoFrameCallback' in v) {
          ;(v as any).cancelVideoFrameCallback(rvfcRef.current)
        } else {
          cancelAnimationFrame(rvfcRef.current)
        }
      }
    }
  }, [])

  useEffect(() => {
    if (!open) return
    setIsLoading(true)
    setError('')
    api.getVideoInfo(projectId, shot.shot_id).then((info) => {
      setFps(info.fps)
      setTotalFrames(info.total_frames)
      setDuration(info.duration)
      setEndFrame(info.total_frames)
      setHasBackup(info.has_backup)
      setIsLoading(false)
    }).catch((e) => {
      setError(e instanceof Error ? e.message : 'Failed to load video info')
      setIsLoading(false)
    })
  }, [open, projectId, shot.shot_id])

  const seekToFrame = (frame: number) => {
    if (videoRef.current && fps > 0) {
      videoRef.current.currentTime = frame / fps
    }
  }

  const handleSliderChange = (value: number) => {
    const clamped = Math.max(minFrames, Math.min(value, totalFrames))
    setEndFrame(clamped)
    seekToFrame(clamped)
  }

  const handleStep = (delta: number) => {
    const next = Math.max(minFrames, Math.min(endFrame + delta, totalFrames))
    setEndFrame(next)
    seekToFrame(next)
  }

  const handleTrim = async () => {
    if (endFrame >= totalFrames) return
    setIsTrimming(true)
    setError('')
    try {
      const result = await api.trimShot(projectId, shot.shot_id, endFrame)
      onTrimmed({
        video_path: result.video_path,
        last_frame_path: result.last_frame_path,
        version: result.version,
      })
      setTotalFrames(result.total_frames)
      setDuration(result.duration)
      setEndFrame(result.total_frames)
      onOpenChange(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Trim failed')
    } finally {
      setIsTrimming(false)
    }
  }

  const handleRestore = async () => {
    setIsRestoring(true)
    setError('')
    try {
      const result = await api.restoreTrim(projectId, shot.shot_id)
      onTrimmed({
        video_path: result.video_path,
        last_frame_path: result.last_frame_path,
        version: result.version,
      })
      setTotalFrames(result.total_frames)
      setDuration(result.duration)
      setEndFrame(result.total_frames)
      setHasBackup(false)
      onOpenChange(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Restore failed')
    } finally {
      setIsRestoring(false)
    }
  }

  const handleAlignTailFrame = async () => {
    setIsAligning(true)
    setError('')
    try {
      const result = await api.alignTailFrame(projectId, shot.shot_id)
      onTrimmed({
        video_path: result.video_path,
        last_frame_path: result.last_frame_path,
        version: result.version,
      })
      setTotalFrames(result.total_frames)
      setDuration(result.duration)
      setEndFrame(result.total_frames)
      setHasBackup(true)
      onOpenChange(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Align failed')
    } finally {
      setIsAligning(false)
    }
  }

  const currentTime = fps > 0 ? (endFrame / fps).toFixed(2) : '0'
  const trimmedPercent = totalFrames > 0 ? (endFrame / totalFrames) * 100 : 100

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl flex flex-col max-h-[90vh]">
        <DialogHeader className="shrink-0">
          <DialogTitle>裁剪视频 — Shot #{shot.shot_id}</DialogTitle>
        </DialogHeader>

        {isLoading ? (
          <div className="flex items-center justify-center py-12">
            <Loader2 className="w-6 h-6 animate-spin text-zinc-400" />
          </div>
        ) : (
          <div className="flex flex-col gap-4 min-h-0">
            {/* Video preview — fills remaining space */}
            <div className="min-h-0 flex-1 flex items-center justify-center bg-black rounded-lg overflow-hidden">
              <video
                ref={videoRef}
                src={shot.video_path || undefined}
                preload="auto"
                className="max-w-full max-h-full object-contain"
                onLoadedMetadata={() => seekToFrame(endFrame)}
                onEnded={stopPreview}
              />
            </div>

            {/* Slider with trim indicator */}
            <div className="shrink-0 space-y-1">
              <div className="relative h-3 bg-zinc-200 rounded-full overflow-hidden">
                <div
                  className="absolute inset-y-0 left-0 bg-blue-500 rounded-full"
                  style={{ width: `${trimmedPercent}%` }}
                />
                <div
                  className="absolute inset-y-0 bg-red-300/50 rounded-r-full"
                  style={{ left: `${trimmedPercent}%`, right: 0 }}
                />
              </div>
              <input
                type="range"
                min={minFrames}
                max={totalFrames}
                value={endFrame}
                onChange={(e) => handleSliderChange(Number(e.target.value))}
                disabled={isPreviewing}
                className="w-full"
              />
            </div>

            {/* Frame info */}
            <div className="shrink-0 flex items-center justify-between text-sm text-zinc-600">
              <span>
                帧: {endFrame} / {totalFrames}
                {endFrame < totalFrames && (
                  <span className="text-red-500 ml-2">
                    裁掉 {totalFrames - endFrame} 帧
                  </span>
                )}
              </span>
              <span>
                时间: {currentTime}s / {duration.toFixed(2)}s
              </span>
            </div>

            {/* Preview + step buttons */}
            <div className="shrink-0 flex items-center gap-1">
              <Button
                variant={isPreviewing ? "default" : "outline"}
                size="sm"
                onClick={handlePreview}
              >
                {isPreviewing ? (
                  <><Square className="w-4 h-4 mr-1" />停止</>
                ) : (
                  <><Play className="w-4 h-4 mr-1" />预览</>
                )}
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => handleStep(-10)}
                disabled={isPreviewing || endFrame <= minFrames}
              >
                <ChevronLeft className="w-4 h-4" />-10
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => handleStep(-1)}
                disabled={isPreviewing || endFrame <= minFrames}
              >
                <ChevronLeft className="w-4 h-4" />-1
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => handleStep(1)}
                disabled={isPreviewing || endFrame >= totalFrames}
              >
                +1<ChevronRight className="w-4 h-4" />
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => handleStep(10)}
                disabled={isPreviewing || endFrame >= totalFrames}
              >
                +10<ChevronRight className="w-4 h-4" />
              </Button>
            </div>

            {/* Actions */}
            <div className="shrink-0 flex items-center justify-between">
              <div className="flex items-center gap-2">
                {hasBackup && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleRestore}
                    disabled={isRestoring || isTrimming || isAligning || isPreviewing}
                  >
                    {isRestoring ? (
                      <><Loader2 className="w-4 h-4 mr-1 animate-spin" />还原中...</>
                    ) : (
                      <><Undo2 className="w-4 h-4 mr-1" />还原</>
                    )}
                  </Button>
                )}
                {shot.target_last_frame_path && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleAlignTailFrame}
                    disabled={isAligning || isTrimming || isRestoring || isPreviewing}
                  >
                    {isAligning ? (
                      <><Loader2 className="w-4 h-4 mr-1 animate-spin" />校准中...</>
                    ) : (
                      <><Crosshair className="w-4 h-4 mr-1" />智能校准</>
                    )}
                  </Button>
                )}
              </div>
              <div className="flex items-center gap-2">
                <Button variant="outline" onClick={() => onOpenChange(false)}>
                  取消
                </Button>
                <Button
                  onClick={handleTrim}
                  disabled={isTrimming || endFrame >= totalFrames}
                >
                  {isTrimming ? (
                    <><Loader2 className="w-4 h-4 mr-1 animate-spin" />裁剪中...</>
                  ) : (
                    '确认裁剪'
                  )}
                </Button>
              </div>
            </div>

            {error && (
              <p className="text-sm text-red-500">{error}</p>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
