import { useCallback, useRef } from 'react'
import { VideoFilmstripTrack } from './VideoFilmstripTrack'
import { AudioWaveformTrack } from './AudioWaveformTrack'
import { frameAtClientX } from '@/lib/waveform'

interface Props {
  spriteUrl: string | null
  peaks: number[] | null
  totalFrames: number
  endFrame: number
  headMuteFrame: number
  speechEndFrame: number | null
  playheadFrame: number | null
  onTrimChange: (frame: number) => void
  onHeadMuteChange: (frame: number) => void
  onHoverFrame: (frame: number | null) => void
}

const V_H = 60
const GAP = 12
const A_H = 84

export function DualTrackTimeline({
  spriteUrl, peaks, totalFrames, endFrame, headMuteFrame, speechEndFrame, playheadFrame,
  onTrimChange, onHeadMuteChange, onHoverFrame,
}: Props) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<null | 'trim' | 'mute'>(null)

  const frac = (f: number) => (totalFrames > 0 ? Math.min(1, Math.max(0, f / totalFrames)) : 0)

  const startDrag = useCallback((kind: 'trim' | 'mute') => (e: React.PointerEvent) => {
    dragRef.current = kind
    e.currentTarget.setPointerCapture?.(e.pointerId)
    const f = wrapRef.current ? frameAtClientX(e.clientX, wrapRef.current, totalFrames) : 0
    if (kind === 'trim') onTrimChange(f)
    else onHeadMuteChange(Math.max(0, Math.min(f, totalFrames)))
  }, [totalFrames, onTrimChange, onHeadMuteChange])

  const onMove = useCallback((e: React.PointerEvent) => {
    if (!dragRef.current || !wrapRef.current) return
    const f = frameAtClientX(e.clientX, wrapRef.current, totalFrames)
    if (dragRef.current === 'trim') onTrimChange(f)
    else onHeadMuteChange(Math.max(0, Math.min(f, totalFrames)))
  }, [totalFrames, onTrimChange, onHeadMuteChange])

  const endDrag = useCallback(() => { dragRef.current = null }, [])

  const hoverMove = useCallback((e: React.PointerEvent) => {
    if (dragRef.current || !wrapRef.current) return
    onHoverFrame(frameAtClientX(e.clientX, wrapRef.current, totalFrames))
  }, [totalFrames, onHoverFrame])

  const cutLeft = `${frac(endFrame) * 100}%`
  const muteLeft = `${frac(headMuteFrame) * 100}%`
  const totalH = V_H + GAP + A_H

  return (
    <div ref={wrapRef} className="relative select-none" style={{ height: totalH }} onPointerMove={onMove} onPointerUp={endDrag} onPointerCancel={endDrag}>
      <div data-testid="video-hover" style={{ height: V_H }} onPointerMove={hoverMove} onPointerLeave={() => onHoverFrame(null)}>
        <VideoFilmstripTrack spriteUrl={spriteUrl} trimFrac={frac(endFrame)} height={V_H} />
      </div>
      <div style={{ height: GAP }} />
      <div style={{ height: A_H }} className="relative">
        <AudioWaveformTrack peaks={peaks} trimFrac={frac(endFrame)} headMuteFrac={frac(headMuteFrame)} speechEndFrac={speechEndFrame != null ? frac(speechEndFrame) : null} height={A_H} />
        {/* 前段静音蓝手柄：只在音频轨 */}
        <div data-testid="headmute-handle" role="slider" aria-label="前段静音"
          onPointerDown={startDrag('mute')} onPointerMove={onMove} onPointerUp={endDrag}
          className="absolute top-0 h-full cursor-ew-resize" style={{ left: muteLeft, width: 16, transform: 'translateX(-8px)' }}>
          <div className="absolute inset-y-0" style={{ left: 7, width: 2.5, background: '#2563EB' }} />
          <div className="absolute" style={{ top: '38%', left: 2, width: 12, height: 40, borderRadius: 6, background: '#2563EB' }} />
        </div>
      </div>
      {/* 裁剪线：贯穿两轨；抓手在中缝 */}
      <div data-testid="cut-line" role="slider" aria-label="裁剪"
        onPointerDown={startDrag('trim')} onPointerMove={onMove} onPointerUp={endDrag}
        className="absolute top-0 cursor-ew-resize" style={{ left: cutLeft, height: totalH, width: 16, transform: 'translateX(-8px)' }}>
        <div className="absolute inset-y-0" style={{ left: 7, width: 3, background: '#EF4444' }} />
        <div className="absolute" style={{ top: V_H + GAP / 2 - 30, left: 1, width: 15, height: 60, borderRadius: 7, background: '#EF4444' }} />
      </div>
      {/* 播放头（预览时贯穿两轨） */}
      {playheadFrame != null && (
        <div data-testid="playhead" className="absolute top-0 pointer-events-none" style={{ left: `${frac(playheadFrame) * 100}%`, height: totalH, width: 2, background: '#15803D' }} />
      )}
    </div>
  )
}
