// frontend-vite/src/components/__tests__/useShotSync.test.tsx
import { renderHook } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { useShotSync } from '../../hooks/useShotSync'

describe('useShotSync', () => {
  it('pauses both elements when video passes trimEndSec', () => {
    const { result } = renderHook(() =>
      useShotSync({ trimEndSec: 2.0, audioEnabled: true }))
    const video = { currentTime: 2.5, pause: vi.fn() }
    const audio = { currentTime: 2.5, pause: vi.fn(), play: vi.fn() }
    // @ts-expect-error test doubles
    result.current.videoRef.current = video
    // @ts-expect-error test doubles
    result.current.audioRef.current = audio
    result.current.onTimeUpdate()
    expect(video.pause).toHaveBeenCalled()
    expect(audio.pause).toHaveBeenCalled()
  })

  it('corrects audio drift > 0.15s on timeupdate', () => {
    const { result } = renderHook(() =>
      useShotSync({ trimEndSec: null, audioEnabled: true }))
    const video = { currentTime: 1.0, pause: vi.fn() }
    const audio = { currentTime: 1.3, pause: vi.fn(), play: vi.fn() }
    // @ts-expect-error test doubles
    result.current.videoRef.current = video
    // @ts-expect-error test doubles
    result.current.audioRef.current = audio
    result.current.onTimeUpdate()
    expect(audio.currentTime).toBeCloseTo(1.0)
  })
})
