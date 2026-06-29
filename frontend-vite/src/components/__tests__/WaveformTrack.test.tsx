import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import WaveformTrack from '../WaveformTrack'

let ctx2d: { clearRect: ReturnType<typeof vi.fn>; fillRect: ReturnType<typeof vi.fn>; fillStyle: string }

beforeEach(() => {
  // jsdom lacks setPointerCapture/releasePointerCapture
  HTMLElement.prototype.setPointerCapture = vi.fn()
  HTMLElement.prototype.releasePointerCapture = vi.fn()

  // canvas 2d context stub
  ctx2d = { clearRect: vi.fn(), fillRect: vi.fn(), fillStyle: '' }
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
    ctx2d as unknown as CanvasRenderingContext2D,
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const samplePeaks = [0.1, 0.8, 0.3, 0.5, 0.2, 0.9, 0.4]

describe('WaveformTrack', () => {
  it('非空 peaks 渲染 canvas 并调用 fillRect', () => {
    render(
      <WaveformTrack
        peaks={samplePeaks}
        totalFrames={240}
        endFrame={240}
        speechEndFrame={180}
        onScrub={() => {}}
      />,
    )
    expect(document.querySelector('canvas')).toBeInTheDocument()
    expect(ctx2d.fillRect).toHaveBeenCalled()
  })

  it('peaks={[]} 降级返回 null — 无 canvas', () => {
    const { container } = render(
      <WaveformTrack
        peaks={[]}
        totalFrames={240}
        endFrame={240}
        speechEndFrame={null}
        onScrub={() => {}}
      />,
    )
    expect(container.querySelector('canvas')).not.toBeInTheDocument()
  })

  it('peaks={null} 加载中 — 显示标签 + 加载提示,无崩溃', () => {
    render(
      <WaveformTrack
        peaks={null}
        totalFrames={240}
        endFrame={240}
        speechEndFrame={null}
        onScrub={() => {}}
      />,
    )
    expect(screen.getByText('声纹波形')).toBeInTheDocument()
    expect(screen.getByText('波形加载中…')).toBeInTheDocument()
  })

  it('点击波形上报对应帧', () => {
    const onScrub = vi.fn()
    render(
      <WaveformTrack
        peaks={samplePeaks}
        totalFrames={240}
        endFrame={240}
        speechEndFrame={180}
        onScrub={onScrub}
      />,
    )
    const canvas = document.querySelector('canvas') as HTMLCanvasElement
    expect(canvas).toBeInTheDocument()
    // jsdom 下 offsetWidth=0,mock 一个尺寸
    Object.defineProperty(canvas, 'offsetWidth', { value: 500, configurable: true })
    fireEvent.pointerDown(canvas, { clientX: 250 })
    expect(onScrub).toHaveBeenCalledWith(120)
  })
})
