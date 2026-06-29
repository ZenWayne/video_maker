// frontend-vite/src/hooks/useShotSync.ts
import { useRef, useCallback } from 'react'

const DRIFT_TOLERANCE = 0.15

export interface ShotSyncOptions {
  trimEndSec: number | null
  audioEnabled: boolean
}

/** Keeps a muted <video> (picture) and an <audio> (vc track) in sync, and
 *  clamps playback to trimEndSec. video is the master clock. */
export function useShotSync({ trimEndSec, audioEnabled }: ShotSyncOptions) {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  const onPlay = useCallback(() => {
    if (audioEnabled) audioRef.current?.play?.()
  }, [audioEnabled])

  const onPause = useCallback(() => {
    audioRef.current?.pause?.()
  }, [])

  const onSeeked = useCallback(() => {
    const v = videoRef.current
    const a = audioRef.current
    if (v && a) a.currentTime = v.currentTime
  }, [])

  const onTimeUpdate = useCallback(() => {
    const v = videoRef.current
    const a = audioRef.current
    if (!v) return
    if (trimEndSec != null && v.currentTime >= trimEndSec) {
      v.pause()
      a?.pause?.()
      return
    }
    if (audioEnabled && a && Math.abs(a.currentTime - v.currentTime) > DRIFT_TOLERANCE) {
      a.currentTime = v.currentTime
    }
  }, [trimEndSec, audioEnabled])

  return { videoRef, audioRef, onPlay, onPause, onSeeked, onTimeUpdate }
}
