// frontend-vite/src/components/ShotPlayer.tsx
import { useState } from 'react'
import { useShotSync } from '../hooks/useShotSync'

export interface ShotPlayerProps {
  videoUrl: string
  trimEndSec: number | null
  audioUrl: string | null
}

/** Non-destructive playback: trims by clamping, substitutes VC audio via a
 *  synced <audio>. A/B toggle switches between vc track and source audio. */
export function ShotPlayer({ videoUrl, trimEndSec, audioUrl }: ShotPlayerProps) {
  const hasVc = !!audioUrl
  const [useVc, setUseVc] = useState(true)        // true = vc track, false = source audio
  const [audioError, setAudioError] = useState(false)
  const audioEnabled = hasVc && useVc && !audioError
  const { videoRef, audioRef, onPlay, onPause, onSeeked, onTimeUpdate } =
    useShotSync({ trimEndSec, audioEnabled })

  function handleAudioError() {
    setAudioError(true)
    setUseVc(false)
  }

  return (
    <div className="shot-player">
      <video
        ref={videoRef}
        src={videoUrl}
        controls
        muted={audioEnabled}
        onPlay={onPlay}
        onPause={onPause}
        onSeeked={onSeeked}
        onTimeUpdate={onTimeUpdate}
        style={{ width: '100%' }}
      />
      {hasVc && (
        <>
          <audio
            ref={audioRef}
            src={audioUrl!}
            muted={!useVc || audioError}
            preload="auto"
            onError={handleAudioError}
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
