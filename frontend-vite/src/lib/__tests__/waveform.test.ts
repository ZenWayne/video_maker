import { describe, it, expect } from 'vitest'
import { downsamplePeaks, frameFromOffsetX, pixelForFrame } from '../waveform'

describe('downsamplePeaks', () => {
  it('降到指定桶数', () => {
    const ch = new Float32Array(1000).fill(0.5)
    expect(downsamplePeaks(ch, 10)).toHaveLength(10)
  })

  it('每桶取绝对值最大', () => {
    const ch = new Float32Array([0.1, -0.9, 0.2, 0.3])
    // 2 桶:[0.1,-0.9] -> 0.9, [0.2,0.3] -> 0.3
    // Float32 精度下值非精确(-0.9 存为 0.89999997),用 toBeCloseTo 逐元素比较
    const peaks = downsamplePeaks(ch, 2)
    expect(peaks).toHaveLength(2)
    expect(peaks[0]).toBeCloseTo(0.9)
    expect(peaks[1]).toBeCloseTo(0.3)
  })

  it('空输入返回全 0 数组', () => {
    expect(downsamplePeaks(new Float32Array(0), 3)).toEqual([0, 0, 0])
  })
})

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
