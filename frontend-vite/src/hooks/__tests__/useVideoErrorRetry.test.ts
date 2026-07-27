// frontend-vite/src/hooks/__tests__/useVideoErrorRetry.test.ts
import { renderHook, act } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { useVideoErrorRetry } from '../useVideoErrorRetry'

describe('useVideoErrorRetry', () => {
  it('calls refetch once when the video errors', () => {
    const refetch = vi.fn()
    const { result } = renderHook(() => useVideoErrorRetry('https://cos.example/a.mp4?sig=1', refetch))

    act(() => result.current.onError())

    expect(refetch).toHaveBeenCalledTimes(1)
  })

  it('does NOT retry a second time without an intervening successful load', () => {
    const refetch = vi.fn()
    const { result } = renderHook(() => useVideoErrorRetry('https://cos.example/a.mp4?sig=1', refetch))

    act(() => result.current.onError())
    act(() => result.current.onError())
    act(() => result.current.onError())

    expect(refetch).toHaveBeenCalledTimes(1)
  })

  // Regression test for I1: q-sign-time regenerates on every signing, so a
  // refetch NEVER returns the same url string — not even for media that is
  // genuinely, permanently broken. The old implementation reset the "already
  // attempted" flag on url identity change alone, which made every refetch
  // look like a fresh chance and turned a permanently-broken file into an
  // unbounded error -> refetch -> error loop (an expensive server-side
  // ffmpeg re-merge for the join-preview caller). This must fail if someone
  // reintroduces a url-keyed reset.
  it('does NOT reset merely because the url changes, with no successful load in between', () => {
    const refetch = vi.fn()
    const { result, rerender } = renderHook(
      ({ url }) => useVideoErrorRetry(url, refetch),
      { initialProps: { url: 'https://cos.example/a.mp4?sig=1' } }
    )

    act(() => result.current.onError())
    expect(refetch).toHaveBeenCalledTimes(1)

    // A freshly-signed url arrives (as it always does on refetch), but the
    // media never actually loaded successfully — still broken.
    rerender({ url: 'https://cos.example/a.mp4?sig=2' })
    act(() => result.current.onError())

    expect(refetch).toHaveBeenCalledTimes(1)
  })

  it('resets the retry budget once onLoad fires (genuine recovery), allowing a later genuine expiry to be rescued too', () => {
    const refetch = vi.fn()
    const { result, rerender } = renderHook(
      ({ url }) => useVideoErrorRetry(url, refetch),
      { initialProps: { url: 'https://cos.example/a.mp4?sig=1' } }
    )

    act(() => result.current.onError())
    expect(refetch).toHaveBeenCalledTimes(1)

    // Freshly-signed url arrives AND the media actually loads this time —
    // caller wires this to onLoadedMetadata/onLoadedData/onCanPlay.
    rerender({ url: 'https://cos.example/a.mp4?sig=2' })
    act(() => result.current.onLoad())

    act(() => result.current.onError())
    expect(refetch).toHaveBeenCalledTimes(2)
  })

  it('does nothing when url is null/undefined', () => {
    const refetch = vi.fn()
    const { result } = renderHook(() => useVideoErrorRetry(null, refetch))

    act(() => result.current.onError())

    expect(refetch).not.toHaveBeenCalled()
  })
})
