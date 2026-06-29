import { useEffect, useRef, useState, useCallback } from 'react'
import { downsamplePeaks, frameFromOffsetX, pixelForFrame } from '@/lib/waveform'

interface WaveformTrackProps {
  videoSrc: string
  totalFrames: number
  endFrame: number
  speechEndFrame: number | null
  onScrub: (frame: number) => void
}

const TRACK_HEIGHT = 84
const BAR_WIDTH = 3
const BAR_GAP = 2
const SILENCE_RATIO = 0.05 // 低于全局峰值 5% 的柱视为静音(灰)
const FALLBACK_WIDTH = 500

type Status = 'loading' | 'ready' | 'unavailable'

export default function WaveformTrack({
  videoSrc,
  totalFrames,
  endFrame,
  speechEndFrame,
  onScrub,
}: WaveformTrackProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const peaksRef = useRef<number[]>([])
  const draggingRef = useRef(false)
  const [status, setStatus] = useState<Status>('loading')

  // ---- 解码音频(videoSrc 变化时)----
  useEffect(() => {
    let cancelled = false
    setStatus('loading')
    const ctx = new AudioContext()
    fetch(videoSrc)
      .then((r) => r.arrayBuffer())
      .then((buf) => ctx.decodeAudioData(buf))
      .then((audio) => {
        if (cancelled) return
        const width = canvasRef.current?.offsetWidth || FALLBACK_WIDTH
        const buckets = Math.max(1, Math.floor(width / (BAR_WIDTH + BAR_GAP)))
        peaksRef.current = downsamplePeaks(audio.getChannelData(0), buckets)
        setStatus('ready')
      })
      .catch(() => {
        if (!cancelled) setStatus('unavailable')
      })
      .finally(() => {
        ctx.close().catch(() => {})
      })
    return () => {
      cancelled = true
    }
  }, [videoSrc])

  // ---- 画 canvas(峰值/状态变化时)----
  const draw = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas || status !== 'ready') return
    const width = canvas.offsetWidth || FALLBACK_WIDTH
    canvas.width = width
    canvas.height = TRACK_HEIGHT
    const g = canvas.getContext('2d')
    if (!g) return
    g.clearRect(0, 0, width, TRACK_HEIGHT)

    const peaks = peaksRef.current
    const globalMax = peaks.reduce((m, p) => (p > m ? p : m), 0) || 1
    const mid = TRACK_HEIGHT / 2

    // 静音高亮带 + 说话结束竖线
    if (speechEndFrame != null) {
      const sx = pixelForFrame(speechEndFrame, width, totalFrames)
      g.fillStyle = 'rgba(252, 211, 77, 0.18)' // amber 18%
      g.fillRect(sx, 0, width - sx, TRACK_HEIGHT)
      g.fillStyle = '#B45309' // amber-700
      g.fillRect(sx - 1, 0, 2, TRACK_HEIGHT)
    }

    // 峰值柱
    const step = BAR_WIDTH + BAR_GAP
    peaks.forEach((p, i) => {
      const norm = p / globalMax
      const h = Math.max(2, norm * (TRACK_HEIGHT - 16))
      g.fillStyle = norm < SILENCE_RATIO ? '#D4D4D8' : '#3B82F6' // zinc-300 / blue-500
      g.fillRect(i * step, mid - h / 2, BAR_WIDTH, h)
    })

    // 待裁区 + 裁剪竖线
    const cx = pixelForFrame(endFrame, width, totalFrames)
    g.fillStyle = 'rgba(239, 68, 68, 0.12)' // red 12%
    g.fillRect(cx, 0, width - cx, TRACK_HEIGHT)
    g.fillStyle = '#EF4444' // red-500
    g.fillRect(cx - 1, 0, 3, TRACK_HEIGHT)
  }, [status, endFrame, speechEndFrame, totalFrames])

  useEffect(() => {
    draw()
  }, [draw])

  // ---- 交互:点击/拖拽设裁剪点 ----
  const scrubTo = useCallback(
    (e: React.PointerEvent<HTMLCanvasElement>) => {
      const width = canvasRef.current?.offsetWidth || FALLBACK_WIDTH
      const rect = canvasRef.current?.getBoundingClientRect()
      const offsetX = rect ? e.clientX - rect.left : 0
      onScrub(frameFromOffsetX(offsetX, width, totalFrames))
    },
    [onScrub, totalFrames],
  )

  if (status === 'unavailable') return null

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold text-zinc-600">声纹波形</span>
        <span className="text-[11px] text-zinc-400">
          蓝=人声 · 黄线=说话结束 · 红线=裁剪点
        </span>
      </div>
      <canvas
        ref={canvasRef}
        style={{ width: '100%', height: TRACK_HEIGHT }}
        className="rounded-lg bg-zinc-50 border border-zinc-200 cursor-ew-resize touch-none"
        onPointerDown={(e) => {
          draggingRef.current = true
          e.currentTarget.setPointerCapture(e.pointerId)
          scrubTo(e)
        }}
        onPointerMove={(e) => {
          if (draggingRef.current) scrubTo(e)
        }}
        onPointerUp={(e) => {
          draggingRef.current = false
          e.currentTarget.releasePointerCapture(e.pointerId)
        }}
      />
      {status === 'loading' && (
        <span className="text-[11px] text-zinc-400">声纹解码中…</span>
      )}
    </div>
  )
}
