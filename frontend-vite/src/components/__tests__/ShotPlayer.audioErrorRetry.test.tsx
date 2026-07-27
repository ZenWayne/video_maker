// frontend-vite/src/components/__tests__/ShotPlayer.audioErrorRetry.test.tsx
//
// The vc <audio> track (vc_audio_url) is signed by the same to_media_url()
// with the same TTL as the video, so it's subject to the exact same
// stale-signed-URL failure. Existing behavior on <audio onError> was to fall
// back to the original track PERMANENTLY (setAudioError(true); setUseVc(false)
// with no retry, no reset) — a signed-URL expiry meant the user silently lost
// their voice-clone audio for the rest of the session. This must retry once
// per audioUrl (mirroring the <video> behavior) and auto-recover once a
// freshly-signed audioUrl actually arrives via our own retry.
import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent, screen } from '@testing-library/react'
import { ShotPlayer } from '../ShotPlayer'

describe('ShotPlayer audio error retry', () => {
  it('calls onVideoError once when the vc <audio> errors', () => {
    const onVideoError = vi.fn()
    const { container } = render(
      <ShotPlayer videoUrl="/v.mp4" trimEndSec={2} audioUrl="/a.wav?sig=1" onVideoError={onVideoError} />
    )
    const audio = container.querySelector('audio') as HTMLAudioElement

    fireEvent.error(audio)

    expect(onVideoError).toHaveBeenCalledTimes(1)
  })

  it('immediately falls back to the original track on error (preserves existing UX)', () => {
    const { container } = render(
      <ShotPlayer videoUrl="/v.mp4" trimEndSec={2} audioUrl="/a.wav?sig=1" />
    )
    const audio = container.querySelector('audio') as HTMLAudioElement
    const video = container.querySelector('video') as HTMLVideoElement

    fireEvent.error(audio)

    expect(video.muted).toBe(false) // unmuted → falls back to source/original audio
    expect(screen.getByTestId('audio-error-msg')).toBeTruthy()
  })

  it('does not retry a second time for the same (still-stale) audioUrl', () => {
    const onVideoError = vi.fn()
    const { container } = render(
      <ShotPlayer videoUrl="/v.mp4" trimEndSec={2} audioUrl="/a.wav?sig=1" onVideoError={onVideoError} />
    )
    const audio = container.querySelector('audio') as HTMLAudioElement

    fireEvent.error(audio)
    fireEvent.error(audio)
    fireEvent.error(audio)

    expect(onVideoError).toHaveBeenCalledTimes(1)
  })

  it('auto-recovers to the vc track once a freshly-signed audioUrl arrives from our own retry', () => {
    const onVideoError = vi.fn()
    const { container, rerender } = render(
      <ShotPlayer videoUrl="/v.mp4" trimEndSec={2} audioUrl="/a.wav?sig=1" onVideoError={onVideoError} />
    )
    let audio = container.querySelector('audio') as HTMLAudioElement
    let video = container.querySelector('video') as HTMLVideoElement

    fireEvent.error(audio)
    expect(video.muted).toBe(false) // fell back to original
    expect(screen.getByTestId('audio-error-msg')).toBeTruthy()

    // Refetch resolved with a freshly-signed audioUrl.
    rerender(
      <ShotPlayer videoUrl="/v.mp4" trimEndSec={2} audioUrl="/a.wav?sig=2" onVideoError={onVideoError} />
    )

    audio = container.querySelector('audio') as HTMLAudioElement
    video = container.querySelector('video') as HTMLVideoElement
    expect(video.muted).toBe(true) // auto-restored to vc audio
    expect(screen.queryByTestId('audio-error-msg')).toBeNull() // error banner cleared
    expect(audio.src).toContain('sig=2')
  })

  it('does NOT auto-restore useVc when audioUrl changes for an unrelated reason (no prior error)', () => {
    // e.g. some other refetch (SSE candidate event) re-signs vc_audio_url —
    // must not silently override a user's manual "原音" toggle choice.
    const { container, rerender } = render(
      <ShotPlayer videoUrl="/v.mp4" trimEndSec={2} audioUrl="/a.wav?sig=1" />
    )
    // User manually switches to 原音 (no error involved).
    fireEvent.click(screen.getByTestId('ab-toggle'))
    let video = container.querySelector('video') as HTMLVideoElement
    expect(video.muted).toBe(false)

    // Some unrelated refetch changes audioUrl.
    rerender(<ShotPlayer videoUrl="/v.mp4" trimEndSec={2} audioUrl="/a.wav?sig=2" />)

    video = container.querySelector('video') as HTMLVideoElement
    expect(video.muted).toBe(false) // still respects the user's manual choice
  })

  it('does NOT retry again when the newly-signed audioUrl fails immediately too, with no successful load in between', () => {
    // q-sign-time regenerates on every signing, so a still-broken audio
    // object gets a freshly-signed (different) url on every refetch too —
    // url identity alone can't distinguish "recovered" from "still broken".
    // This is the I1 regression: the old code reset the attempt flag purely
    // because audioUrl changed, so this scenario retried forever.
    const onVideoError = vi.fn()
    const { container, rerender } = render(
      <ShotPlayer videoUrl="/v.mp4" trimEndSec={2} audioUrl="/a.wav?sig=1" onVideoError={onVideoError} />
    )
    let audio = container.querySelector('audio') as HTMLAudioElement
    fireEvent.error(audio)
    expect(onVideoError).toHaveBeenCalledTimes(1)

    rerender(
      <ShotPlayer videoUrl="/v.mp4" trimEndSec={2} audioUrl="/a.wav?sig=2" onVideoError={onVideoError} />
    )
    audio = container.querySelector('audio') as HTMLAudioElement
    fireEvent.error(audio) // genuinely broken this time too, no load in between

    expect(onVideoError).toHaveBeenCalledTimes(1)
    const video = container.querySelector('video') as HTMLVideoElement
    expect(video.muted).toBe(false) // fell back again
  })

  it('retries again after the recovered audioUrl actually loads (onLoadedData) and later genuinely fails again', () => {
    const onVideoError = vi.fn()
    const { container, rerender } = render(
      <ShotPlayer videoUrl="/v.mp4" trimEndSec={2} audioUrl="/a.wav?sig=1" onVideoError={onVideoError} />
    )
    let audio = container.querySelector('audio') as HTMLAudioElement
    fireEvent.error(audio)
    expect(onVideoError).toHaveBeenCalledTimes(1)

    rerender(
      <ShotPlayer videoUrl="/v.mp4" trimEndSec={2} audioUrl="/a.wav?sig=2" onVideoError={onVideoError} />
    )
    audio = container.querySelector('audio') as HTMLAudioElement
    fireEvent.loadedData(audio) // the freshly-signed url actually played
    fireEvent.error(audio) // a later, genuine new expiry

    expect(onVideoError).toHaveBeenCalledTimes(2)
  })
})
