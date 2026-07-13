import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { ShotPlayer } from '../ShotPlayer'

// jsdom video/audio 元素 currentTime 可写；模拟 timeupdate 时的静音切换
describe('ShotPlayer 前段静音', () => {
  it('currentTime < headMuteSec 时音轨静音，越过后取消', () => {
    const { container } = render(
      <ShotPlayer videoUrl="/v.mp4" trimEndSec={null} audioUrl="/a.wav" headMuteSec={1.0} />
    )
    const video = container.querySelector('video') as HTMLVideoElement
    const audio = container.querySelector('audio') as HTMLAudioElement
    // 位于静音区
    Object.defineProperty(video, 'currentTime', { value: 0.5, configurable: true })
    video.dispatchEvent(new Event('timeupdate'))
    expect(audio.muted).toBe(true)
    // 越过静音区
    Object.defineProperty(video, 'currentTime', { value: 1.5, configurable: true })
    video.dispatchEvent(new Event('timeupdate'))
    expect(audio.muted).toBe(false)
  })

  it('headMuteSec=null 时不干预静音', () => {
    const { container } = render(
      <ShotPlayer videoUrl="/v.mp4" trimEndSec={null} audioUrl="/a.wav" headMuteSec={null} />
    )
    const video = container.querySelector('video') as HTMLVideoElement
    const audio = container.querySelector('audio') as HTMLAudioElement
    Object.defineProperty(video, 'currentTime', { value: 0.1, configurable: true })
    video.dispatchEvent(new Event('timeupdate'))
    expect(audio.muted).toBe(false)
  })
})
