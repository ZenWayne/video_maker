// frontend-vite/src/components/__tests__/ShotPlayer.test.tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { ShotPlayer } from '../ShotPlayer'

describe('ShotPlayer', () => {
  it('mode 1: plain video, no audio element, no toggle', () => {
    const { container } = render(<ShotPlayer videoUrl="/v.mp4" trimEndSec={null} audioUrl={null} />)
    expect(container.querySelector('video')).toBeTruthy()
    expect(container.querySelector('audio')).toBeNull()
    expect(screen.queryByTestId('ab-toggle')).toBeNull()
  })

  it('mode 3: muted video + audio element + A/B toggle when audioUrl set', () => {
    const { container } = render(<ShotPlayer videoUrl="/v.mp4" trimEndSec={2} audioUrl="/a.wav" />)
    const video = container.querySelector('video') as HTMLVideoElement
    expect(video.muted).toBe(true)
    expect(container.querySelector('audio')).toBeTruthy()
    expect(screen.getByTestId('ab-toggle')).toBeTruthy()
  })

  it('A/B toggle mutes vc audio and unmutes source', () => {
    const { container } = render(<ShotPlayer videoUrl="/v.mp4" trimEndSec={2} audioUrl="/a.wav" />)
    const video = container.querySelector('video') as HTMLVideoElement
    fireEvent.click(screen.getByTestId('ab-toggle'))   // 切到原音
    expect(video.muted).toBe(false)
    const audio = container.querySelector('audio') as HTMLAudioElement
    expect(audio.muted).toBe(true)
  })

  it('audio error falls back to source audio and shows error message', () => {
    const { container } = render(<ShotPlayer videoUrl="/v.mp4" trimEndSec={2} audioUrl="/a.wav" />)
    const audio = container.querySelector('audio') as HTMLAudioElement
    const video = container.querySelector('video') as HTMLVideoElement

    // Initially video should be muted (vc track active)
    expect(video.muted).toBe(true)

    // Fire an error event on the audio element
    fireEvent.error(audio)

    // Video should become unmuted (fallback to source audio)
    expect(video.muted).toBe(false)

    // Error message should appear
    expect(screen.getByTestId('audio-error-msg')).toBeTruthy()
    expect(screen.getByTestId('audio-error-msg').textContent).toContain('配音音轨加载失败')
  })

  it('toggling back to 配音 mid-playback resumes the vc audio, not just unmutes it', () => {
    // The regression: the A/B toggle only flipped `muted`, while the <audio>'s
    // play/pause state was driven solely by the video's play event.  Switching
    // to 原音, playing, then back to 配音 left the vc audio paused → muted video
    // + paused audio = silence.  Confirmed in a real browser: after the switch
    // v.muted=true, a.muted=false, but a.paused stayed true.
    const { container } = render(<ShotPlayer videoUrl="/v.mp4" trimEndSec={2} audioUrl="/a.wav" />)
    const video = container.querySelector('video') as HTMLVideoElement
    const audio = container.querySelector('audio') as HTMLAudioElement
    vi.spyOn(video, 'play').mockResolvedValue(undefined as unknown as void)
    vi.spyOn(video, 'pause').mockImplementation(() => {})
    const audioPlay = vi.spyOn(audio, 'play').mockResolvedValue(undefined as unknown as void)
    vi.spyOn(audio, 'pause').mockImplementation(() => {})

    fireEvent.click(screen.getByTestId('ab-toggle'))   // → 原音 (audioEnabled false)
    fireEvent.play(video)                              // start playback; vc audio stays paused
    audioPlay.mockClear()

    fireEvent.click(screen.getByTestId('ab-toggle'))   // → 配音 while the video is playing
    expect(audioPlay).toHaveBeenCalled()               // vc audio must resume, not just unmute
  })

  it('timeline shows the TRIMMED duration, not the full source', () => {
    render(<ShotPlayer videoUrl="/v.mp4" trimEndSec={125} audioUrl={null} />)
    // 125s → 2:05; the custom timeline is scaled to trimEndSec
    expect(screen.getByTestId('time-label').textContent).toContain('/ 2:05')
  })
})
