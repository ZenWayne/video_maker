// frontend-vite/src/hooks/__tests__/useVideoErrorRetry.test.ts
import { renderHook, act } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { useVideoErrorRetry } from '../useVideoErrorRetry'

describe('useVideoErrorRetry', () => {
  it('calls refetch once when the video errors', () => {
    const refetch = vi.fn()
    const { result } = renderHook(() => useVideoErrorRetry('https://cos.example/a.mp4?sig=1', refetch))

    act(() => result.current())

    expect(refetch).toHaveBeenCalledTimes(1)
  })

  it('does NOT retry a second time for the same URL (avoids infinite loop on genuinely broken media)', () => {
    const refetch = vi.fn()
    const { result } = renderHook(() => useVideoErrorRetry('https://cos.example/a.mp4?sig=1', refetch))

    act(() => result.current())
    act(() => result.current())
    act(() => result.current())

    expect(refetch).toHaveBeenCalledTimes(1)
  })

  it('resets the retry flag once the url changes, allowing the next expiry to be rescued too', () => {
    const refetch = vi.fn()
    const { result, rerender } = renderHook(
      ({ url }) => useVideoErrorRetry(url, refetch),
      { initialProps: { url: 'https://cos.example/a.mp4?sig=1' } }
    )

    act(() => result.current())
    expect(refetch).toHaveBeenCalledTimes(1)

    // A second error on the SAME (still-stale) url must not retry again.
    act(() => result.current())
    expect(refetch).toHaveBeenCalledTimes(1)

    // Freshly-signed URL arrives (e.g. refetch resolved and updated the prop).
    rerender({ url: 'https://cos.example/a.mp4?sig=2' })

    // A later expiry on the NEW url can be rescued once more.
    act(() => result.current())
    expect(refetch).toHaveBeenCalledTimes(2)
  })

  it('does nothing when url is null/undefined', () => {
    const refetch = vi.fn()
    const { result } = renderHook(() => useVideoErrorRetry(null, refetch))

    act(() => result.current())

    expect(refetch).not.toHaveBeenCalled()
  })
})
