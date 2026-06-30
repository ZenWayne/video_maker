// frontend-vite/src/components/ShotPlayer.tsx
import { useCallback, useState } from 'react'
import { useShotSync } from '../hooks/useShotSync'

export interface ShotPlayerProps {
  videoUrl: string
  trimEndSec: number | null
  audioUrl: string | null
  poster?: string | null
}

function fmt(t: number): string {
  if (!Number.isFinite(t) || t < 0) t = 0
  const m = Math.floor(t / 60)
  const s = Math.floor(t % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

/** Non-destructive playback. The source video is the full immutable clip, so we
 *  present a CUSTOM timeline scaled to the trimmed range (trimEndSec): the
 *  progress bar, time label and seeking are all bounded to it, and playback stops
 *  at the trim point — native <video controls> would show the full source length. */
export function ShotPlayer({ videoUrl, trimEndSec, audioUrl, poster }: ShotPlayerProps) {
  const hasVc = !!audioUrl
  const [useVc, setUseVc] = useState(true)
  const [audioError, setAudioError] = useState(false)
  const audioEnabled = hasVc && useVc && !audioError

  const [playing, setPlaying] = useState(false)
  const [current, setCurrent] = useState(0)
  const [fullDuration, setFullDuration] = useState(0)

  const { videoRef, audioRef, onPlay, onPause, onSeeked, onTimeUpdate } =
    useShotSync({ trimEndSec, audioEnabled })

  // effective end = the trim point when set, else the full source duration
  const end = trimEndSec != null && trimEndSec > 0 ? trimEndSec : fullDuration

  const togglePlay = useCallback(() => {
    const v = videoRef.current
    if (!v) return
    if (v.paused) {
      if (end && v.currentTime >= end - 0.02) v.currentTime = 0 // replay from start
      v.play()
    } else {
      v.pause()
    }
  }, [end])

  const seekFrac = useCallback((frac: number) => {
    const v = videoRef.current
    if (!v || !end) return
    v.currentTime = Math.max(0, Math.min(frac, 1)) * end
  }, [end])

  const handleTimeUpdate = useCallback(() => {
    onTimeUpdate() // clamp to trimEndSec + keep vc audio in sync
    const v = videoRef.current
    if (v) setCurrent(Math.min(v.currentTime, end || v.currentTime))
  }, [onTimeUpdate, end])

  const frac = end > 0 ? Math.min(current / end, 1) : 0

  return (
    <div className="shot-player">
      <div className="relative">
        <video
          ref={videoRef}
          src={videoUrl}
          poster={poster ?? undefined}
          preload="none"
          muted={audioEnabled}
          onLoadedMetadata={(e) => setFullDuration((e.currentTarget as HTMLVideoElement).duration)}
          onPlay={() => { onPlay(); setPlaying(true) }}
          onPause={() => { onPause(); setPlaying(false) }}
          onSeeked={onSeeked}
          onTimeUpdate={handleTimeUpdate}
          onClick={togglePlay}
          className="w-full block"
        />
        {/* prominent center play button while paused (matches the thumbnail) */}
        {!playing && (
          <button
            type="button"
            data-testid="center-play"
            onClick={togglePlay}
            aria-label="播放"
            className="absolute inset-0 flex items-center justify-center bg-black/25 group-hover:bg-black/15 transition-colors"
          >
            <span className="flex items-center justify-center w-14 h-14 rounded-full bg-black/45">
              <svg viewBox="0 0 24 24" className="w-7 h-7 fill-white ml-0.5"><path d="M8 5v14l11-7z" /></svg>
            </span>
          </button>
        )}
      </div>

      {/* custom controls scaled to the trimmed (effective) duration */}
      <div className="flex items-center gap-2 mt-1">
        <button
          type="button"
          data-testid="play-toggle"
          onClick={togglePlay}
          className="text-sm px-2 py-0.5 rounded bg-gray-100"
        >
          {playing ? '⏸' : '▶'}
        </button>
        <div
          data-testid="seek-track"
          className="relative flex-1 h-1.5 bg-gray-200 rounded cursor-pointer"
          onClick={(e) => {
            const r = e.currentTarget.getBoundingClientRect()
            seekFrac((e.clientX - r.left) / r.width)
          }}
        >
          <div className="absolute inset-y-0 left-0 bg-blue-500 rounded" style={{ width: `${frac * 100}%` }} />
        </div>
        <span data-testid="time-label" className="text-xs tabular-nums text-gray-600">
          {fmt(current)} / {fmt(end)}
        </span>
      </div>

      {hasVc && (
        <>
          <audio
            ref={audioRef}
            src={audioUrl!}
            muted={!useVc || audioError}
            preload="auto"
            onError={() => { setAudioError(true); setUseVc(false) }}
          />
          {audioError && (
            <p data-testid="audio-error-msg" className="text-xs text-red-500 mt-1">
              配音音轨加载失败，已回退原音
            </p>
          )}
          <button
            type="button"
            data-testid="ab-toggle"
            onClick={() => setUseVc((v) => !v)}
            className="text-xs px-2 py-1 mt-1 rounded bg-gray-100"
          >
            {useVc && !audioError ? '🔊 配音' : '🎙 原音'}
          </button>
        </>
      )}
    </div>
  )
}
