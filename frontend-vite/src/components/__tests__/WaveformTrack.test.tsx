import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import WaveformTrack from '../WaveformTrack'

// --- Web Audio / fetch / canvas 在 jsdom 中不存在,需 mock ---
const decodeAudioData = vi.fn()
const close = vi.fn().mockResolvedValue(undefined)

beforeEach(() => {
  decodeAudioData.mockReset()
  close.mockReset().mockResolvedValue(undefined)

  // jsdom lacks setPointerCapture/releasePointerCapture
  HTMLElement.prototype.setPointerCapture = vi.fn()
  HTMLElement.prototype.releasePointerCapture = vi.fn()

  vi.stubGlobal(
    'AudioContext',
    vi.fn().mockImplementation(function () { return { decodeAudioData, close } }),
  )
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)) }),
  )
  // canvas 2d context stub
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
    clearRect: vi.fn(),
    fillRect: vi.fn(),
    fillStyle: '',
  } as unknown as CanvasRenderingContext2D)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function fakeBuffer(): AudioBuffer {
  return {
    numberOfChannels: 1,
    length: 100,
    sampleRate: 24000,
    duration: 100 / 24000,
    getChannelData: () => new Float32Array(100).fill(0.5),
  } as unknown as AudioBuffer
}

describe('WaveformTrack', () => {
  it('解码成功后渲染 canvas', async () => {
    decodeAudioData.mockResolvedValue(fakeBuffer())
    render(
      <WaveformTrack
        videoSrc="/fake/video.mp4"
        totalFrames={240}
        endFrame={240}
        speechEndFrame={180}
        onScrub={() => {}}
      />,
    )
    await waitFor(() =>
      expect(document.querySelector('canvas')).toBeInTheDocument(),
    )
  })

  it('点击波形上报对应帧', async () => {
    decodeAudioData.mockResolvedValue(fakeBuffer())
    const onScrub = vi.fn()
    render(
      <WaveformTrack
        videoSrc="/fake/video.mp4"
        totalFrames={240}
        endFrame={240}
        speechEndFrame={180}
        onScrub={onScrub}
      />,
    )
    const canvas = await waitFor(() => {
      const c = document.querySelector('canvas')
      if (!c) throw new Error('no canvas')
      return c as HTMLCanvasElement
    })
    // jsdom 下 offsetWidth=0,组件应有兜底宽度;mock 一个尺寸
    Object.defineProperty(canvas, 'offsetWidth', { value: 500, configurable: true })
    fireEvent.pointerDown(canvas, { clientX: 250 })
    expect(onScrub).toHaveBeenCalled()
  })

  it('解码失败时降级隐藏(返回 null,无 canvas)', async () => {
    decodeAudioData.mockRejectedValue(new Error('no audio track'))
    const { container } = render(
      <WaveformTrack
        videoSrc="/fake/video.mp4"
        totalFrames={240}
        endFrame={240}
        speechEndFrame={null}
        onScrub={() => {}}
      />,
    )
    await waitFor(() =>
      expect(container.querySelector('canvas')).not.toBeInTheDocument(),
    )
  })
})
