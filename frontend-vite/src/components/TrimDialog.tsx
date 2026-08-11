import { useEffect, useRef, useState, useCallback } from 'react'
import { Loader2, ChevronLeft, ChevronRight, Play, Square, Undo2, Crosshair, AudioLines, VolumeX } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { api } from '@/lib/api'
import type { AspectRatio, Shot } from '@/lib/types'
import { DualTrackTimeline } from './trim/DualTrackTimeline'
import { useVideoErrorRetry } from '../hooks/useVideoErrorRetry'

interface TrimDialogProps {
  shot: Shot
  projectId: string
  /** ShotsPage → ShotCard 一路传下来；组件内目前未消费，保留以免影响调用方 */
  aspectRatio?: AspectRatio
  open: boolean
  onOpenChange: (open: boolean) => void
  onTrimmed: (updates: {
    video_path: string
    last_frame_path: string
    trim_frames: number | null
    trim_end_sec: number | null
    version: number
    next_shot?: { shot_id: number; custom_first_frame_path: string }
  }) => void
  /** 前段静音(audio_head_mute_frames)保存后回调,与 onTrimmed 正交、独立触发。 */
  onShotUpdated?: (shotId: number, updates: Partial<Shot>) => void
}

export function TrimDialog({
  shot,
  projectId,
  open,
  onOpenChange,
  onTrimmed,
  onShotUpdated,
}: TrimDialogProps) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [fps, setFps] = useState(24)
  const [totalFrames, setTotalFrames] = useState(0)
  const [duration, setDuration] = useState(0)
  const [endFrame, setEndFrame] = useState(0)
  const [speechEndFrame, setSpeechEndFrame] = useState<number | null>(null)
  const [sourceVideoUrl, setSourceVideoUrl] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isTrimming, setIsTrimming] = useState(false)
  const [isRestoring, setIsRestoring] = useState(false)
  const [isAligning, setIsAligning] = useState(false)
  const [isDetectingSilence, setIsDetectingSilence] = useState(false)
  const [isDetectingSpeechStart, setIsDetectingSpeechStart] = useState(false)
  const [headMuteFrame, setHeadMuteFrame] = useState(0)
  const [peaks, setPeaks] = useState<number[] | null>(null)
  const [spriteUrl, setSpriteUrl] = useState<string | null>(null)
  const [notice, setNotice] = useState('')
  const [isPreviewing, setIsPreviewing] = useState(false)
  const [playheadFrame, setPlayheadFrame] = useState<number | null>(null)
  const [hasBackup, setHasBackup] = useState(false)
  const [error, setError] = useState('')
  const minFrames = 24

  const rvfcRef = useRef<number>(0)
  // 载入时的前段静音帧,用于判断确认时是否需要保存(与 trim 正交、独立 PUT)
  const initialHeadMuteFrameRef = useRef(0)
  // 载入时的裁剪点(trim_frames),用于判断确认时是否需要调用 trimShot(与前段静音正交、独立 PUT)
  const initialEndFrameRef = useRef(0)

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
    // 保留 playheadFrame(暂停位置)以便续播;清除交给重新打开/播完
  }, [])

  const handlePreview = useCallback(() => {
    if (isPreviewing) {
      stopPreview()
      return
    }
    const v = videoRef.current
    if (!v || fps <= 0) return
    const endSec = endFrame / fps
    // 从头播 or 续播:已到/超过裁剪点(或尚未开始)→ 从 0;否则从当前暂停处续播
    if (v.currentTime >= endSec - 0.5 / fps || v.currentTime <= 0.001) {
      v.currentTime = 0
    }
    v.play()
    setIsPreviewing(true)
    setPlayheadFrame(Math.round(v.currentTime * fps))

    const useRvfc = 'requestVideoFrameCallback' in v
    const tick = () => {
      const vid = videoRef.current
      if (!vid) return
      if (!useRvfc && vid.paused) return
      setPlayheadFrame(Math.round(vid.currentTime * fps))
      // 播到裁剪点即停(留半帧余量避免过冲)
      if (vid.currentTime >= endSec - 0.5 / fps) {
        vid.pause()
        setIsPreviewing(false)
        setPlayheadFrame(null) // 播完清除播放头
        return
      }
      rvfcRef.current = useRvfc
        ? (vid as any).requestVideoFrameCallback(tick)
        : requestAnimationFrame(tick)
    }
    rvfcRef.current = useRvfc
      ? (v as any).requestVideoFrameCallback(tick)
      : requestAnimationFrame(tick)
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
    setNotice('')
    setPeaks(null)
    setPlayheadFrame(null)
    const loadedHeadMuteFrame = shot.audio_head_mute_frames ?? 0
    setHeadMuteFrame(loadedHeadMuteFrame)
    initialHeadMuteFrameRef.current = loadedHeadMuteFrame
    api.getVideoInfo(projectId, shot.shot_id).then((info) => {
      setFps(info.fps)
      setTotalFrames(info.total_frames)
      setDuration(info.duration)
      // Reflect the current (non-destructive) trim point, not the full source length
      const loadedEndFrame = shot.trim_frames ?? info.total_frames
      setEndFrame(loadedEndFrame)
      initialEndFrameRef.current = loadedEndFrame
      setHasBackup(info.has_backup)
      setSpeechEndFrame(info.speech_end_frame)
      setSourceVideoUrl(info.source_video_url ?? null)
      setIsLoading(false)
    }).catch((e) => {
      setError(e instanceof Error ? e.message : 'Failed to load video info')
      setIsLoading(false)
    })
    api.getWaveform(projectId, shot.shot_id).then((r) => setPeaks(r.peaks)).catch(() => setPeaks([]))
    api.getFilmstrip(projectId, shot.shot_id).then((r) => setSpriteUrl(r.url)).catch(() => setSpriteUrl(null))
  }, [open, projectId, shot.shot_id])

  // 签名 URL 过期兜底：只重拉 source_video_url 换新签名，不动其它已加载的
  // 裁剪/静音编辑状态（headMuteFrame/endFrame/peaks 等）——用户可能正在编辑，
  // 过期只是网络层的事，不该把编辑进度一起冲掉。
  const refreshSourceUrl = useCallback(async () => {
    if (!projectId) return
    try {
      const info = await api.getVideoInfo(projectId, shot.shot_id)
      setSourceVideoUrl(info.source_video_url ?? null)
    } catch {
      // 静默失败——video 保持错误态，用户可关闭再重新打开裁剪弹窗重试
    }
  }, [projectId, shot.shot_id])

  const { onError: handleVideoError, onLoad: handleVideoLoaded } = useVideoErrorRetry(
    sourceVideoUrl ?? shot.video_path ?? null,
    refreshSourceUrl
  )

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
    setIsTrimming(true)
    setError('')
    try {
      // 裁剪与前段静音正交、各自独立 PUT:只调用值发生变化的那个接口
      if (endFrame !== initialEndFrameRef.current) {
        const result = await api.trimShot(projectId, shot.shot_id, endFrame)
        initialEndFrameRef.current = endFrame
        onTrimmed({
          video_path: result.video_path,
          last_frame_path: result.last_frame_path,
          trim_frames: result.trim_frames,
          trim_end_sec: result.trim_end_sec,
          version: result.version,
          next_shot: result.next_shot,
        })
        setTotalFrames(result.total_frames)
        setDuration(result.duration)
        setEndFrame(result.total_frames)
      }

      // 前段静音与裁剪正交:独立 PUT,不阻塞/不依赖 trim 结果
      let headMuteOk = true
      if (headMuteFrame !== initialHeadMuteFrameRef.current) {
        try {
          const muteResult = await api.setAudioHeadMute(projectId, shot.shot_id, headMuteFrame)
          initialHeadMuteFrameRef.current = headMuteFrame
          onShotUpdated?.(shot.shot_id, {
            audio_head_mute_frames: muteResult.audio_head_mute_frames,
            audio_head_mute_sec: muteResult.audio_head_mute_sec,
          })
        } catch (e) {
          headMuteOk = false
          setError(e instanceof Error ? e.message : '前段静音保存失败')
        }
      }

      // 前段静音保存失败时保持对话框打开,让错误提示可见;裁剪本身已成功,状态更新照常执行
      if (headMuteOk) {
        onOpenChange(false)
      }
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
        trim_frames: result.trim_frames,
        trim_end_sec: result.trim_end_sec,
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
        trim_frames: result.trim_frames,
        trim_end_sec: result.trim_end_sec,
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

  const handleDetectSilence = async () => {
    setIsDetectingSilence(true)
    setError('')
    setNotice('')
    try {
      const result = await api.detectSilence(projectId, shot.shot_id)
      if (result.has_silence && result.suggested_end_frame != null) {
        setEndFrame(result.suggested_end_frame)
        seekToFrame(result.suggested_end_frame)
      } else {
        setNotice('无尾部静音可裁剪')
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Silence detect failed')
    } finally {
      setIsDetectingSilence(false)
    }
  }

  const handleDetectSpeechStart = async () => {
    setIsDetectingSpeechStart(true)
    setError('')
    setNotice('')
    try {
      const r = await api.detectSpeechStart(projectId, shot.shot_id)
      if (r.has_lead_silence && r.suggested_start_frame != null) {
        setHeadMuteFrame(r.suggested_start_frame)
      } else {
        setNotice('未检测到开头静音')
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '检测失败')
    } finally {
      setIsDetectingSpeechStart(false)
    }
  }

  const currentTime = fps > 0 ? (endFrame / fps).toFixed(2) : '0'

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
                src={sourceVideoUrl ?? shot.video_path ?? undefined}
                preload="auto"
                className="max-w-full max-h-full object-contain"
                onLoadedMetadata={() => {
                  seekToFrame(endFrame)
                  handleVideoLoaded()
                }}
                onEnded={stopPreview}
                onError={handleVideoError}
              />
            </div>

            {/* Hover hint — 常驻提示,解释视频轨 hover 的擦洗预览行为 */}
            <p className="shrink-0 text-[11px] text-zinc-400">
              指针在视频轨上移动 → 预览器跳到该帧
            </p>

            {/* Dual-track timeline — 视频胶片轨 + 音频波形轨,共享同一裁剪线 */}
            <div className="shrink-0">
              <DualTrackTimeline
                spriteUrl={spriteUrl}
                peaks={peaks}
                totalFrames={totalFrames}
                endFrame={endFrame}
                headMuteFrame={headMuteFrame}
                speechEndFrame={speechEndFrame}
                playheadFrame={playheadFrame}
                onTrimChange={handleSliderChange}
                onHeadMuteChange={(f) => setHeadMuteFrame(Math.max(0, Math.min(f, totalFrames)))}
                onHoverFrame={(f) => { if (f != null && !isPreviewing) seekToFrame(f) }}
              />
            </div>

            {/* Legend — 解码双轨的每种颜色/线条,消解"在裁视频还是动音频"的歧义 */}
            <div className="shrink-0 flex flex-col gap-0.5 text-[11px] text-zinc-500">
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
                <span className="flex items-center gap-1">
                  <span className="inline-block w-3 h-3 rounded-sm" style={{ background: '#3B82F6' }} />
                  人声
                </span>
                <span className="flex items-center gap-1">
                  <span className="inline-block w-3 h-3 rounded-sm bg-zinc-300" />
                  静音/已裁剪
                </span>
                <span className="flex items-center gap-1">
                  <span className="inline-block w-0.5 h-3" style={{ background: '#EF4444' }} />
                  裁剪(视频+音频一起裁)
                </span>
              </div>
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
                <span className="flex items-center gap-1">
                  <span className="inline-block w-0.5 h-3" style={{ background: '#2563EB' }} />
                  开头静音手柄(只静音音频,不裁帧)
                </span>
                <span className="flex items-center gap-1">
                  <span className="inline-block w-0.5 h-3" style={{ background: '#F59E0B' }} />
                  说话结束
                </span>
                <span className="flex items-center gap-1">
                  <span className="inline-block w-0.5 h-3" style={{ background: '#15803D' }} />
                  播放头(预览时)
                </span>
              </div>
            </div>

            {/* Frame info */}
            <div className="shrink-0 flex flex-wrap items-center justify-between gap-x-4 gap-y-0.5 text-sm text-zinc-600">
              <span className="whitespace-nowrap">
                帧: {endFrame} / {totalFrames}
                {endFrame < totalFrames && (
                  <span className="text-red-500 ml-2">
                    裁掉 {totalFrames - endFrame} 帧
                  </span>
                )}
                {speechEndFrame != null && (
                  <span className="text-amber-700 ml-2 font-medium">
                    静音参考: 第 {speechEndFrame} 帧
                  </span>
                )}
                {headMuteFrame > 0 && (
                  <span className="text-blue-600 ml-2">
                    前段静音: 前 {headMuteFrame} 帧 / {(headMuteFrame / fps).toFixed(2)}s
                  </span>
                )}
                {playheadFrame != null && (
                  <span className="text-green-700 ml-2">
                    ▶ 播放 {Math.min(playheadFrame + 1, totalFrames)}
                  </span>
                )}
              </span>
              <span className="whitespace-nowrap">
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
                    disabled={isRestoring || isTrimming || isAligning || isPreviewing || isDetectingSilence || isDetectingSpeechStart}
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
                    disabled={isAligning || isTrimming || isRestoring || isPreviewing || isDetectingSilence || isDetectingSpeechStart}
                  >
                    {isAligning ? (
                      <><Loader2 className="w-4 h-4 mr-1 animate-spin" />校准中...</>
                    ) : (
                      <><Crosshair className="w-4 h-4 mr-1" />智能校准</>
                    )}
                  </Button>
                )}
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleDetectSilence}
                  disabled={isDetectingSilence || isTrimming || isAligning || isRestoring || isPreviewing || isDetectingSpeechStart}
                >
                  {isDetectingSilence ? (
                    <><Loader2 className="w-4 h-4 mr-1 animate-spin" />检测中...</>
                  ) : (
                    <><AudioLines className="w-4 h-4 mr-1" />静音裁剪</>
                  )}
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleDetectSpeechStart}
                  disabled={isDetectingSpeechStart || isTrimming || isAligning || isRestoring || isPreviewing || isDetectingSilence}
                >
                  {isDetectingSpeechStart ? (
                    <><Loader2 className="w-4 h-4 mr-1 animate-spin" />检测中...</>
                  ) : (
                    <><VolumeX className="w-4 h-4 mr-1" />检测开头静音</>
                  )}
                </Button>
              </div>
              <div className="flex items-center gap-2">
                <Button variant="outline" onClick={() => onOpenChange(false)}>
                  取消
                </Button>
                <Button
                  onClick={handleTrim}
                  disabled={isTrimming || isDetectingSilence || isDetectingSpeechStart}
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
            {notice && !error && (
              <p className="text-sm text-zinc-500">{notice}</p>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
