import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { TrimDialog } from '../TrimDialog'
import type { Shot } from '@/lib/types'

// Mock api module
vi.mock('@/lib/api', () => ({
  api: {
    getVideoInfo: vi.fn().mockResolvedValue({
      fps: 24,
      total_frames: 240,
      duration: 10.0,
      has_backup: false,
      speech_end_frame: 180,
      speech_end_sec: 7.5,
    }),
    trimShot: vi.fn(),
  },
}))

const mockShot: Shot = {
  id: 1,
  project_id: 'proj-1',
  shot_id: 1,
  text: 'Test shot',
  shot_type: 'Medium Shot',
  visual_description: 'desc',
  shot_duration: 4,
  status: 'completed',
  align_with_previous: false,
  use_prev_last_frame: false,
  motion_prompt: null,
  first_frame_path: null,
  video_path: '/fake/video.mp4',
  last_frame_path: null,
  word_count_warning: false,
  error_message: null,
  custom_first_frame_path: null,
  custom_reference_paths: null,
  reference_image_hint: null,
  vc_status: null,
  vc_error_message: null,
  cc_status: null,
  cc_error_message: null,
  target_last_frame_path: null,
  tf_status: null,
  tf_error_message: null,
  tf_confirmed: false,
  auto_trim: true,
}

// Manual rAF control — lets us tick the checkEnd loop explicitly
let rafCallbacks: FrameRequestCallback[] = []
let rafIdCounter = 0
const cancelledIds = new Set<number>()

function flushRAF() {
  const cbs = [...rafCallbacks]
  rafCallbacks = []
  cbs.forEach((cb) => cb(performance.now()))
}

beforeEach(() => {
  rafCallbacks = []
  rafIdCounter = 0
  cancelledIds.clear()

  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    const id = ++rafIdCounter
    rafCallbacks.push((...args) => {
      if (!cancelledIds.has(id)) cb(...args)
    })
    return id
  })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => {
    cancelledIds.add(id)
  })

  // play/pause must flip the `paused` property — checkEnd reads it
  HTMLMediaElement.prototype.play = vi.fn(function (this: HTMLMediaElement) {
    Object.defineProperty(this, 'paused', { value: false, writable: true, configurable: true })
    return Promise.resolve()
  })
  HTMLMediaElement.prototype.pause = vi.fn(function (this: HTMLMediaElement) {
    Object.defineProperty(this, 'paused', { value: true, writable: true, configurable: true })
  })

  // Stubs for WaveformTrack (AudioContext / fetch / canvas)
  vi.stubGlobal('AudioContext', vi.fn().mockImplementation(function() {
    return {
      decodeAudioData: vi.fn().mockResolvedValue({
        numberOfChannels: 1, length: 10, sampleRate: 24000, duration: 0,
        getChannelData: () => new Float32Array(10).fill(0.3),
      }),
      close: vi.fn().mockResolvedValue(undefined),
    }
  }))
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)),
  }))
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
    clearRect: vi.fn(), fillRect: vi.fn(), fillStyle: '',
  } as unknown as CanvasRenderingContext2D)
})

afterEach(() => {
  vi.restoreAllMocks()
})

function getVideo(): HTMLVideoElement {
  return document.querySelector('video')!
}

/** Render dialog and wait for video info to load */
async function renderReady() {
  const onOpenChange = vi.fn()
  const onTrimmed = vi.fn()

  render(
    <TrimDialog
      shot={mockShot}
      projectId="proj-1"
      open={true}
      onOpenChange={onOpenChange}
      onTrimmed={onTrimmed}
    />
  )

  await waitFor(() => {
    expect(screen.getByText(/帧: 240 \/ 240/)).toBeInTheDocument()
  })

  return { onOpenChange, onTrimmed }
}

describe('TrimDialog — preview trimmed result before confirming', () => {
  it('plays from t=0 to endFrame when preview is clicked', async () => {
    await renderReady()
    const video = getVideo()

    // Trim to frame 120 → endTime = 120/24 = 5.0s
    fireEvent.change(screen.getByRole('slider'), { target: { value: '120' } })

    fireEvent.click(screen.getByText('预览').closest('button')!)

    expect(video.currentTime).toBe(0) // reset to start
    expect(video.play).toHaveBeenCalledTimes(1)
  })

  it('auto-stops at the endFrame boundary', async () => {
    await renderReady()
    const video = getVideo()

    // Trim to frame 120 → endTime = 5.0s
    fireEvent.change(screen.getByRole('slider'), { target: { value: '120' } })
    fireEvent.click(screen.getByText('预览').closest('button')!)

    // Simulate playback: currentTime hasn't reached boundary yet
    Object.defineProperty(video, 'currentTime', {
      value: 3.0,
      writable: true,
      configurable: true,
    })
    act(() => flushRAF())
    // Should NOT have paused — still before boundary
    expect(video.pause).not.toHaveBeenCalled()
    expect(screen.getByText('停止')).toBeInTheDocument()

    // Simulate playback reaching the boundary
    video.currentTime = 5.0
    act(() => flushRAF())

    // Should auto-pause near the endFrame boundary
    expect(video.pause).toHaveBeenCalledTimes(1)

    // Button reverts to "预览"
    await waitFor(() => {
      expect(screen.getByText('预览')).toBeInTheDocument()
    })
  })

  it('auto-stops when currentTime overshoots endFrame', async () => {
    await renderReady()
    const video = getVideo()

    // Trim to frame 72 → endTime = 3.0s
    fireEvent.change(screen.getByRole('slider'), { target: { value: '72' } })
    fireEvent.click(screen.getByText('预览').closest('button')!)

    Object.defineProperty(video, 'currentTime', {
      value: 3.05, // slightly past boundary (browser rAF granularity)
      writable: true,
      configurable: true,
    })
    act(() => flushRAF())

    expect(video.pause).toHaveBeenCalledTimes(1)
  })

  it('manual stop pauses mid-preview and resumes controls', async () => {
    await renderReady()
    const video = getVideo()

    fireEvent.change(screen.getByRole('slider'), { target: { value: '120' } })
    fireEvent.click(screen.getByText('预览').closest('button')!)

    // Controls should be locked during preview
    expect(screen.getByRole('slider')).toBeDisabled()
    expect(screen.getByText('-1').closest('button')).toBeDisabled()

    // Click stop
    fireEvent.click(screen.getByText('停止').closest('button')!)
    expect(video.pause).toHaveBeenCalled()

    // Controls should be re-enabled
    await waitFor(() => {
      expect(screen.getByText('预览')).toBeInTheDocument()
    })
    expect(screen.getByRole('slider')).not.toBeDisabled()
    expect(screen.getByText('-1').closest('button')).not.toBeDisabled()
  })

  it('does not trim until user clicks confirm — preview is non-destructive', async () => {
    const { onTrimmed } = await renderReady()
    const video = getVideo()

    // Trim slider to frame 120
    fireEvent.change(screen.getByRole('slider'), { target: { value: '120' } })

    // Preview the trim
    fireEvent.click(screen.getByText('预览').closest('button')!)
    Object.defineProperty(video, 'currentTime', {
      value: 5.0,
      writable: true,
      configurable: true,
    })
    act(() => flushRAF())

    // Preview finished — but no trim API call was made
    expect(onTrimmed).not.toHaveBeenCalled()
    // Confirm button is still available for the actual trim
    expect(screen.getByText('确认裁剪').closest('button')).not.toBeDisabled()
  })

  it('preview uses updated endFrame after step buttons', async () => {
    await renderReady()
    const video = getVideo()

    // Step down 10 frames: 240 → 230, endTime = 230/24 ≈ 9.583s
    fireEvent.click(screen.getByText('-10').closest('button')!)
    expect(screen.getByText(/帧: 230 \/ 240/)).toBeInTheDocument()

    fireEvent.click(screen.getByText('预览').closest('button')!)
    expect(video.currentTime).toBe(0)
    expect(video.play).toHaveBeenCalled()

    // At 9.0s — still before boundary, should keep playing
    Object.defineProperty(video, 'currentTime', {
      value: 9.0,
      writable: true,
      configurable: true,
    })
    act(() => flushRAF())
    expect(video.pause).not.toHaveBeenCalled()

    // At 9.59s — past 230/24 = 9.583s, should stop
    video.currentTime = 9.59
    act(() => flushRAF())
    expect(video.pause).toHaveBeenCalledTimes(1)
  })

  it('confirm button stays disabled until slider is moved', async () => {
    await renderReady()

    // Initially endFrame == totalFrames → nothing to trim
    expect(screen.getByText('确认裁剪').closest('button')).toBeDisabled()

    // Move slider → now there's something to trim
    fireEvent.change(screen.getByRole('slider'), { target: { value: '200' } })
    expect(screen.getByText('确认裁剪').closest('button')).not.toBeDisabled()

    // Move back to max → nothing to trim again
    fireEvent.change(screen.getByRole('slider'), { target: { value: '240' } })
    expect(screen.getByText('确认裁剪').closest('button')).toBeDisabled()
  })

  it('加载后渲染声纹波形轨', async () => {
    render(
      <TrimDialog
        shot={mockShot}
        projectId="proj-1"
        open={true}
        onOpenChange={() => {}}
        onTrimmed={() => {}}
      />,
    )
    expect(await screen.findByText('声纹波形')).toBeInTheDocument()
  })
})
