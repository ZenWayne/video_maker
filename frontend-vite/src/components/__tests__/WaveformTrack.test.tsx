import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import WaveformTrack from '../WaveformTrack'

let ctx2d: { clearRect: ReturnType<typeof vi.fn>; fillRect: ReturnType<typeof vi.fn>; fillStyle: string }
// 记录每次 fillRect 时的 fillStyle,用于断言画了哪些颜色(如绿色播放头)
let fillStyleLog: string[]

beforeEach(() => {
  // jsdom lacks setPointerCapture/releasePointerCapture
  HTMLElement.prototype.setPointerCapture = vi.fn()
  HTMLElement.prototype.releasePointerCapture = vi.fn()

  // canvas 2d context stub — fillRect 记录当时的 fillStyle
  fillStyleLog = []
  ctx2d = {
    clearRect: vi.fn(),
    fillRect: vi.fn(() => { fillStyleLog.push(ctx2d.fillStyle) }),
    fillStyle: '',
  }
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

  it('playheadFrame 非空时绘制绿色播放头线', () => {
    render(
      <WaveformTrack
        peaks={samplePeaks}
        totalFrames={240}
        endFrame={240}
        speechEndFrame={null}
        playheadFrame={60}
        onScrub={() => {}}
      />,
    )
    expect(fillStyleLog).toContain('#15803D') // green-700 播放头
  })

  it('playheadFrame 缺省时不绘制播放头', () => {
    render(
      <WaveformTrack
        peaks={samplePeaks}
        totalFrames={240}
        endFrame={240}
        speechEndFrame={null}
        onScrub={() => {}}
      />,
    )
    expect(fillStyleLog).not.toContain('#15803D')
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

  it('endFrame 右侧画磨砂灰覆盖(已裁剪灰显)，不再画红膜', () => {
    render(
      <WaveformTrack
        peaks={samplePeaks}
        totalFrames={240}
        endFrame={200}
        speechEndFrame={180}
        onScrub={() => {}}
      />,
    )
    expect(fillStyleLog).toContain('rgba(244, 244, 245, 0.78)')
    expect(fillStyleLog).not.toContain('rgba(239, 68, 68, 0.12)')
  })

  it('图例说明包含 灰=已裁剪', () => {
    render(
      <WaveformTrack
        peaks={samplePeaks}
        totalFrames={240}
        endFrame={200}
        speechEndFrame={null}
        onScrub={() => {}}
      />,
    )
    expect(
      screen.getByText('蓝=人声 · 灰=已裁剪 · 黄线=说话结束 · 红线=裁剪点 · 绿线=播放'),
    ).toBeInTheDocument()
  })

  it('speechEndFrame 在已裁剪区内时,黄线绘制在灰显之上保持清晰可见', () => {
    render(
      <WaveformTrack
        peaks={samplePeaks}
        totalFrames={240}
        endFrame={100}
        speechEndFrame={180}
        onScrub={() => {}}
      />,
    )
    const greyOverlayIndex = fillStyleLog.indexOf('rgba(244, 244, 245, 0.78)')
    const amberLineIndex = fillStyleLog.indexOf('#F59E0B')
    expect(greyOverlayIndex).toBeGreaterThan(-1)
    expect(amberLineIndex).toBeGreaterThan(-1)
    expect(amberLineIndex).toBeGreaterThan(greyOverlayIndex) // 黄线在灰显之后绘制
  })

  it('headMuteFrame>0 时画蓝色前手柄线 + 左侧淡蓝遮罩', () => {
    render(
      <WaveformTrack peaks={samplePeaks} totalFrames={240} endFrame={240}
        speechEndFrame={null} headMuteFrame={30} onScrub={() => {}} onHeadMuteScrub={() => {}} />,
    )
    // 蓝色前手柄线 #3B82F6 与淡蓝遮罩 rgba(59, 130, 246, 0.14) 均被绘制
    expect(fillStyleLog).toContain('#2563EB')          // 前手柄竖线 blue-600
    expect(fillStyleLog).toContain('rgba(37, 99, 235, 0.14)')  // 左侧遮罩
  })

  it('headMuteFrame 缺省或为 0 时不绘制前手柄', () => {
    render(
      <WaveformTrack peaks={samplePeaks} totalFrames={240} endFrame={240}
        speechEndFrame={null} onScrub={() => {}} />,
    )
    expect(fillStyleLog).not.toContain('#2563EB')
    expect(fillStyleLog).not.toContain('rgba(37, 99, 235, 0.14)')
  })

  it('点击波形左侧 15% 区域内且传入 onHeadMuteScrub 时上报前手柄帧', () => {
    const onScrub = vi.fn()
    const onHeadMuteScrub = vi.fn()
    render(
      <WaveformTrack peaks={samplePeaks} totalFrames={240} endFrame={240}
        speechEndFrame={null} headMuteFrame={0} onScrub={onScrub} onHeadMuteScrub={onHeadMuteScrub} />,
    )
    const canvas = document.querySelector('canvas') as HTMLCanvasElement
    Object.defineProperty(canvas, 'offsetWidth', { value: 500, configurable: true })
    // offsetX=50 → 500*0.15=75,落在左 15% 区
    fireEvent.pointerDown(canvas, { clientX: 50 })
    expect(onHeadMuteScrub).toHaveBeenCalledWith(24)
    expect(onScrub).not.toHaveBeenCalled()
  })

  it('点击波形中段(非前手柄区)且传入 onHeadMuteScrub 时仍走 onScrub', () => {
    const onScrub = vi.fn()
    const onHeadMuteScrub = vi.fn()
    render(
      <WaveformTrack peaks={samplePeaks} totalFrames={240} endFrame={240}
        speechEndFrame={null} headMuteFrame={0} onScrub={onScrub} onHeadMuteScrub={onHeadMuteScrub} />,
    )
    const canvas = document.querySelector('canvas') as HTMLCanvasElement
    Object.defineProperty(canvas, 'offsetWidth', { value: 500, configurable: true })
    fireEvent.pointerDown(canvas, { clientX: 250 })
    expect(onScrub).toHaveBeenCalledWith(120)
    expect(onHeadMuteScrub).not.toHaveBeenCalled()
  })
})
