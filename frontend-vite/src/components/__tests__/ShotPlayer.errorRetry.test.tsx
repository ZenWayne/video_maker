// frontend-vite/src/components/__tests__/ShotPlayer.errorRetry.test.tsx
//
// Signed COS URLs carry a TTL; a long-open page can hit a stale URL and get
// a 403 → <video onError>. ShotPlayer must call the parent-supplied
// onVideoError once per distinct videoUrl to fetch a fresh signed URL, and
// must NOT hardcode or reason about the TTL itself — it only reacts to the
// error event.
import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { ShotPlayer } from '../ShotPlayer'

describe('ShotPlayer video error retry', () => {
  it('calls onVideoError once when the <video> errors', () => {
    const onVideoError = vi.fn()
    const { container } = render(
      <ShotPlayer videoUrl="/v.mp4?sig=1" trimEndSec={null} audioUrl={null} onVideoError={onVideoError} />
    )
    const video = container.querySelector('video') as HTMLVideoElement

    fireEvent.error(video)

    expect(onVideoError).toHaveBeenCalledTimes(1)
  })

  it('does not retry a second time for the same (still-stale) videoUrl', () => {
    const onVideoError = vi.fn()
    const { container } = render(
      <ShotPlayer videoUrl="/v.mp4?sig=1" trimEndSec={null} audioUrl={null} onVideoError={onVideoError} />
    )
    const video = container.querySelector('video') as HTMLVideoElement

    fireEvent.error(video)
    fireEvent.error(video)
    fireEvent.error(video)

    expect(onVideoError).toHaveBeenCalledTimes(1)
  })

  it('retries again after videoUrl changes (freshly-signed URL arrives)', () => {
    const onVideoError = vi.fn()
    const { container, rerender } = render(
      <ShotPlayer videoUrl="/v.mp4?sig=1" trimEndSec={null} audioUrl={null} onVideoError={onVideoError} />
    )
    let video = container.querySelector('video') as HTMLVideoElement
    fireEvent.error(video)
    expect(onVideoError).toHaveBeenCalledTimes(1)

    rerender(
      <ShotPlayer videoUrl="/v.mp4?sig=2" trimEndSec={null} audioUrl={null} onVideoError={onVideoError} />
    )
    video = container.querySelector('video') as HTMLVideoElement
    fireEvent.error(video)

    expect(onVideoError).toHaveBeenCalledTimes(2)
  })

  it('does not throw when onVideoError is not supplied', () => {
    const { container } = render(
      <ShotPlayer videoUrl="/v.mp4?sig=1" trimEndSec={null} audioUrl={null} />
    )
    const video = container.querySelector('video') as HTMLVideoElement
    expect(() => fireEvent.error(video)).not.toThrow()
  })
})
