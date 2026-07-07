// frontend-vite/src/hooks/useShotSync.ts
import { useRef, useCallback } from 'react'

const DRIFT_TOLERANCE = 0.15

export interface ShotSyncOptions {
  trimEndSec: number | null
  audioEnabled: boolean
  headMuteSec?: number | null
}

/** Keeps a muted <video> (picture) and an <audio> (vc track) in sync, and
 *  clamps playback to trimEndSec. video is the master clock. */
export function useShotSync({ trimEndSec, audioEnabled, headMuteSec = null }: ShotSyncOptions) {
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
      v?.pause?.()
      a?.pause?.()
      return
    }
    if (audioEnabled && a && Math.abs(a.currentTime - v.currentTime) > DRIFT_TOLERANCE) {
      a.currentTime = v.currentTime
    }
    if (headMuteSec != null) {
      const inMute = v.currentTime < headMuteSec
      // 生效音轨：有 vc 用 audio，否则视频自带音轨
      if (audioEnabled && a) a.muted = inMute
      else v.muted = inMute
    }
  }, [trimEndSec, audioEnabled, headMuteSec])

  return { videoRef, audioRef, onPlay, onPause, onSeeked, onTimeUpdate }
}
