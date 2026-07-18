import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { DualTrackTimeline } from '../trim/DualTrackTimeline'

beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => {
    const ctx: any = { fillRect: () => {}, clearRect: () => {}, fillStyle: '' }
    return ctx
  })
  // 容器测宽 400px，从 x=0 开始
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    left: 0, width: 400, right: 400, top: 0, bottom: 100, height: 100, x: 0, y: 0, toJSON: () => ({}),
  } as DOMRect)
})
afterEach(() => vi.restoreAllMocks())

function base() {
  return {
    spriteUrl: '/s.png', peaks: [0.5, 0.6, 0.7], totalFrames: 240, endFrame: 240,
    headMuteFrame: 0, speechEndFrame: null, playheadFrame: null,
    onTrimChange: vi.fn(), onHeadMuteChange: vi.fn(), onHoverFrame: vi.fn(),
  }
}

describe('DualTrackTimeline', () => {
  it('渲染视频轨 + 音频轨 + 裁剪线', () => {
    render(<DualTrackTimeline {...base()} />)
    expect(screen.getByTestId('video-track')).toBeTruthy()
    expect(screen.getByTestId('audio-track')).toBeTruthy()
    expect(screen.getByTestId('cut-line')).toBeTruthy()
  })
  it('拖裁剪线上报新裁剪帧（x=200/400 → 120 帧）', () => {
    const p = base()
    render(<DualTrackTimeline {...p} />)
    const line = screen.getByTestId('cut-line')
    fireEvent.pointerDown(line, { clientX: 200 })
    fireEvent.pointerMove(line, { clientX: 200 })
    expect(p.onTrimChange).toHaveBeenCalledWith(120)
  })
  it('拖前段静音手柄上报静音帧', () => {
    const p = { ...base(), headMuteFrame: 24 }
    render(<DualTrackTimeline {...p} />)
    const handle = screen.getByTestId('headmute-handle')
    fireEvent.pointerDown(handle, { clientX: 40 })
    fireEvent.pointerMove(handle, { clientX: 40 })
    expect(p.onHeadMuteChange).toHaveBeenCalledWith(24) // 40/400*240=24
  })
  it('hover 视频轨上报 hover 帧，离开上报 null', () => {
    const p = base()
    render(<DualTrackTimeline {...p} />)
    const hover = screen.getByTestId('video-hover') // hover 包裹（onPointerLeave 不冒泡，须直接命中它）
    fireEvent.pointerMove(hover, { clientX: 100 })
    expect(p.onHoverFrame).toHaveBeenCalledWith(60) // 100/400*240
    fireEvent.pointerLeave(hover)
    expect(p.onHoverFrame).toHaveBeenCalledWith(null)
  })
  it('渲染视频/音频轨道标签（左缘叠加，不占布局宽度）', () => {
    render(<DualTrackTimeline {...base()} />)
    expect(screen.getByTestId('track-label-video')).toHaveTextContent('视频')
    expect(screen.getByTestId('track-label-audio')).toHaveTextContent('音频')
  })
  it('裁剪线与前段静音手柄落点相近时，手柄仍可被抓取（手柄层级须高于裁剪线）', () => {
    // 两者落点都在 x=200（240 帧的 120 帧处），验证手柄不被裁剪线的 z 序盖住
    const p = { ...base(), endFrame: 120, headMuteFrame: 120 }
    render(<DualTrackTimeline {...p} />)
    const handle = screen.getByTestId('headmute-handle')
    const cutLine = screen.getByTestId('cut-line')
    // 手柄的 z-index class 须严格高于裁剪线（cut-line 后渲染的贯穿全高线不能盖住手柄）
    expect(handle.className).toMatch(/\bz-10\b/)
    expect(cutLine.className).toMatch(/\bz-0\b/)
    // 拖手柄仍应正常上报静音帧变化，而不是被裁剪线抢走指针
    fireEvent.pointerDown(handle, { clientX: 210 })
    expect(p.onHeadMuteChange).toHaveBeenCalledWith(126) // 210/400*240
  })
})
