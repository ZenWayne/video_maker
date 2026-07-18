import { describe, it, expect } from 'vitest'
import { frameFromOffsetX, pixelForFrame, frameAtClientX } from '../waveform'

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

function fakeEl(left: number, width: number): HTMLElement {
  return { getBoundingClientRect: () => ({ left, width, right: left + width, top: 0, bottom: 0, height: 0, x: left, y: 0, toJSON: () => ({}) }) } as unknown as HTMLElement
}

describe('frameAtClientX', () => {
  it('maps clientX within the element to a frame', () => {
    const el = fakeEl(100, 200) // track spans x=100..300
    expect(frameAtClientX(100, el, 240)).toBe(0)      // left edge
    expect(frameAtClientX(200, el, 240)).toBe(120)    // middle
    expect(frameAtClientX(300, el, 240)).toBe(240)    // right edge
  })
  it('clamps outside the element', () => {
    const el = fakeEl(100, 200)
    expect(frameAtClientX(40, el, 240)).toBe(0)
    expect(frameAtClientX(999, el, 240)).toBe(240)
  })
})
