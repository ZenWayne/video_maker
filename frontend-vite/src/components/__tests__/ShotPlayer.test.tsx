// frontend-vite/src/components/__tests__/ShotPlayer.test.tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
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
})
