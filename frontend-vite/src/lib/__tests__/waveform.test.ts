import { describe, it, expect } from 'vitest'
import { frameFromOffsetX, pixelForFrame } from '../waveform'

describe('frameFromOffsetX', () => {
  it('左缘 → 0 帧', () => {
    expect(frameFromOffsetX(0, 500, 240)).toBe(0)
  })

  it('右缘 → totalFrames', () => {
    expect(frameFromOffsetX(500, 500, 240)).toBe(240)
  })

  it('中点 → 一半帧(四舍五入)', () => {
    expect(frameFromOffsetX(250, 500, 240)).toBe(120)
  })

  it('越界钳制', () => {
    expect(frameFromOffsetX(-50, 500, 240)).toBe(0)
    expect(frameFromOffsetX(999, 500, 240)).toBe(240)
  })
})

describe('pixelForFrame', () => {
  it('与 frameFromOffsetX 互逆(端点)', () => {
    expect(pixelForFrame(0, 500, 240)).toBe(0)
    expect(pixelForFrame(240, 500, 240)).toBe(500)
  })
})
