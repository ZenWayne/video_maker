import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { VideoFilmstripTrack } from '../trim/VideoFilmstripTrack'

describe('VideoFilmstripTrack', () => {
  it('spriteUrl 存在时用作背景，不显示降级块', () => {
    render(<VideoFilmstripTrack spriteUrl="/strip.png" trimFrac={0.8} />)
    expect(screen.getByTestId('video-track')).toBeTruthy()
    expect(screen.queryByTestId('video-track-fallback')).toBeNull()
  })
  it('spriteUrl 为 null 时降级为纯色块', () => {
    render(<VideoFilmstripTrack spriteUrl={null} trimFrac={0.8} />)
    expect(screen.getByTestId('video-track-fallback')).toBeTruthy()
  })
  it('裁掉遮罩宽度 = (1-trimFrac)', () => {
    render(<VideoFilmstripTrack spriteUrl="/strip.png" trimFrac={0.75} />)
    const ov = screen.getByTestId('video-trim-overlay') as HTMLElement
    expect(ov.style.width).toBe('25%')
  })
})
