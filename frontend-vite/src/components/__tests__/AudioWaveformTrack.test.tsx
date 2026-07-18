import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { AudioWaveformTrack } from '../trim/AudioWaveformTrack'

let fillStyleLog: string[] = []
beforeEach(() => {
  fillStyleLog = []
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => {
    const ctx: any = { fillRect: () => fillStyleLog.push(ctx.fillStyle), clearRect: () => {}, fillStyle: '' }
    return ctx
  })
})
afterEach(() => vi.restoreAllMocks())

describe('AudioWaveformTrack', () => {
  it('非空 peaks 渲染 canvas 并画条', () => {
    render(<AudioWaveformTrack peaks={[0.1, 0.8, 0.5, 0.9]} trimFrac={1} headMuteFrac={0} speechEndFrac={null} />)
    expect(screen.getByTestId('audio-track')).toBeTruthy()
    expect(fillStyleLog.some((c) => c.toUpperCase() === '#3B82F6')).toBe(true)
  })
  it('headMuteFrac>0 画淡蓝前段染', () => {
    render(<AudioWaveformTrack peaks={[0.5, 0.6]} trimFrac={1} headMuteFrac={0.2} speechEndFrac={null} />)
    expect(fillStyleLog.some((c) => c.toUpperCase().startsWith('#2563EB'))).toBe(true)
  })
  it('peaks 为 null 显示加载态', () => {
    render(<AudioWaveformTrack peaks={null} trimFrac={1} headMuteFrac={0} speechEndFrac={null} />)
    expect(screen.getByTestId('audio-track')).toBeTruthy()
  })
})
