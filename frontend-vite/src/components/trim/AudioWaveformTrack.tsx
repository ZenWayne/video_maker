import { useEffect, useRef } from 'react'

interface Props {
  peaks: number[] | null
  trimFrac: number
  headMuteFrac: number
  speechEndFrac: number | null
  height?: number
}

const VOICED = '#3B82F6'
const SILENCE = '#D4D4D8'
const MUTE_TINT = '#2563EB24'
const FROST = '#F4F4F5CC'
const AMBER = '#F59E0B'

/** 音频波形轨（纯展示）：波形 + 裁掉灰显 + 前段静音染 + 说话结束黄线。指针由容器负责。 */
export function AudioWaveformTrack({ peaks, trimFrac, headMuteFrac, speechEndFrac, height = 84 }: Props) {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const cv = ref.current
    if (!cv || !peaks) return
    const w = cv.offsetWidth || 500
    const h = height
    cv.width = w
    cv.height = h
    const ctx = cv.getContext('2d')
    if (!ctx) return
    ctx.clearRect(0, 0, w, h)
    const n = peaks.length
    const bw = n > 0 ? w / n : w
    for (let i = 0; i < n; i++) {
      const v = peaks[i]
      const bh = Math.max(2, v * (h - 12))
      ctx.fillStyle = v < 0.05 ? SILENCE : VOICED
      ctx.fillRect(i * bw + bw * 0.15, (h - bh) / 2, Math.max(1, bw * 0.7), bh)
    }
    // 右侧裁掉灰显
    const trimX = trimFrac * w
    ctx.fillStyle = FROST
    ctx.fillRect(trimX, 0, w - trimX, h)
    // 左侧前段静音染
    if (headMuteFrac > 0) {
      ctx.fillStyle = MUTE_TINT
      ctx.fillRect(0, 0, headMuteFrac * w, h)
    }
    // 说话结束黄线（画在灰显之上）
    if (speechEndFrac != null) {
      ctx.fillStyle = AMBER
      ctx.fillRect(speechEndFrac * w - 1, 0, 2, h)
    }
  }, [peaks, trimFrac, headMuteFrac, speechEndFrac, height])

  return (
    <div data-testid="audio-track" className="relative rounded-md bg-zinc-50 border border-zinc-200 overflow-hidden" style={{ height }}>
      {peaks === null ? (
        <div className="absolute inset-0 flex items-center justify-center text-[11px] text-zinc-400">加载波形…</div>
      ) : (
        <canvas ref={ref} className="w-full block" style={{ height }} />
      )}
    </div>
  )
}
