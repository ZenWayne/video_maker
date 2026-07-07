import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { TrimDialog } from '../TrimDialog'
import { api } from '@/lib/api'
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
      source_video_url: '/api/media/projects/p/shots/shot_1/output_1_a.mp4',
    }),
    getWaveform: vi.fn().mockResolvedValue({ peaks: [0.2, 0.6, 0.4, 0.8, 0.3] }),
    trimShot: vi.fn(),
    detectSpeechStart: vi.fn(),
    setAudioHeadMute: vi.fn().mockResolvedValue({
      shot_id: 1, audio_head_mute_frames: null, audio_head_mute_sec: null,
    }),
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
  video_path: '/fake/video.mp4',
  last_frame_path: null,
  word_count_warning: false,
  error_message: null,
  custom_first_frame_path: null,
  ff_status: null,
  ff_error_message: null,
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
  image_candidates: [],
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

  // canvas 2d context stub (WaveformTrack draws to canvas)
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
    clearRect: vi.fn(), fillRect: vi.fn(), fillStyle: '',
  } as unknown as CanvasRenderingContext2D)
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
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

/** Render dialog with an optional shot override (inline render, no wait) */
function renderDialog(shotOverride?: Shot) {
  const onOpenChange = vi.fn()
  const onTrimmed = vi.fn()

  render(
    <TrimDialog
      shot={shotOverride ?? mockShot}
      projectId="proj-1"
      open={true}
      onOpenChange={onOpenChange}
      onTrimmed={onTrimmed}
    />
  )

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

  it('停止后再次预览从暂停处续播(不回到 0),并显示播放帧数', async () => {
    await renderReady()
    const video = getVideo()

    // 裁剪到 120 帧 → endSec = 5.0s
    fireEvent.change(screen.getByRole('slider'), { target: { value: '120' } })

    // 开始预览(从头 → 归零)
    fireEvent.click(screen.getByText('预览').closest('button')!)
    expect(video.currentTime).toBe(0)

    // 播到 2.5s(第 60 帧)——未到裁剪点
    Object.defineProperty(video, 'currentTime', { value: 2.5, writable: true, configurable: true })
    act(() => flushRAF())

    // 播放帧数显示当前帧
    expect(screen.getByText(/播放\s*61/)).toBeInTheDocument()

    // 手动停止 → 暂停位置保留(计数器仍在)
    fireEvent.click(screen.getByText('停止').closest('button')!)
    expect(video.pause).toHaveBeenCalled()
    expect(screen.getByText(/播放\s*61/)).toBeInTheDocument()

    // 再次预览 → 从 2.5s 续播,不回到 0
    fireEvent.click(screen.getByText('预览').closest('button')!)
    expect(video.currentTime).toBe(2.5)
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

  it('confirm button is enabled at full length (last frame = untrimmed)', async () => {
    await renderReady()

    // Initially endFrame == totalFrames (full length) → button is ENABLED
    // (last frame means "not trimmed" — confirm at full length clears the trim)
    expect(screen.getByText('确认裁剪').closest('button')).not.toBeDisabled()

    // Move slider to trim → button stays enabled
    fireEvent.change(screen.getByRole('slider'), { target: { value: '200' } })
    expect(screen.getByText('确认裁剪').closest('button')).not.toBeDisabled()

    // Move back to max (full length) → button still enabled
    fireEvent.change(screen.getByRole('slider'), { target: { value: '240' } })
    expect(screen.getByText('确认裁剪').closest('button')).not.toBeDisabled()
  })

  it('confirm button is enabled when already-trimmed shot loads at full timeline', async () => {
    // Render a shot that was previously trimmed to frame 200
    renderDialog({ ...mockShot, trim_frames: 200 })

    // Wait for load — total timeline is 240, but endFrame initialized to trim_frames (200)
    await waitFor(() => {
      expect(screen.getByText(/帧:\s*200\s*\/\s*240/)).toBeInTheDocument()
    })

    // Confirm button is enabled (can always confirm, even at non-full-length)
    expect(screen.getByText('确认裁剪').closest('button')).not.toBeDisabled()
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
    // 确认波形数据确实经 api.getWaveform 拉取(而非仅渲染 loading 标签)
    await waitFor(() =>
      expect(api.getWaveform).toHaveBeenCalledWith('proj-1', 1),
    )
  })

  it('帧信息行常显静音参考帧数(琥珀色)', async () => {
    renderDialog()
    expect(await screen.findByText('静音参考: 第 180 帧')).toBeInTheDocument()
  })

  it('预览 video 使用源片 URL 而非 shot.video_path', async () => {
    renderDialog()
    await screen.findByText(/帧:/)
    const video = document.querySelector('video')!
    expect(video.getAttribute('src')).toBe('/api/media/projects/p/shots/shot_1/output_1_a.mp4')
  })

  it('已裁剪 shot 打开时时间轴仍是全段、裁剪点在 trim_frames', async () => {
    renderDialog({ ...mockShot, trim_frames: 200 })
    expect(await screen.findByText(/帧:\s*200\s*\/\s*240/)).toBeInTheDocument()
    expect(screen.getByText('裁掉 40 帧')).toBeInTheDocument()
    const slider = document.querySelector('input[type="range"]') as HTMLInputElement
    expect(slider.max).toBe('240')
  })

  it('点击"检测开头静音"命中前段静音后帧信息显示"前段静音"', async () => {
    vi.mocked(api.detectSpeechStart).mockResolvedValueOnce({
      has_lead_silence: true,
      suggested_start_frame: 30,
      fps: 24,
      total_frames: 240,
      duration: 10.0,
    })
    renderDialog()
    await screen.findByText(/帧:/)

    // 检测前不显示前段静音信息
    expect(screen.queryByText(/前段静音/)).not.toBeInTheDocument()

    fireEvent.click(screen.getByText('检测开头静音').closest('button')!)

    expect(await screen.findByText(/前段静音: 前 30 帧/)).toBeInTheDocument()
    expect(api.detectSpeechStart).toHaveBeenCalledWith('proj-1', 1)
  })

  it('未检测到开头静音时提示"未检测到开头静音"', async () => {
    vi.mocked(api.detectSpeechStart).mockResolvedValueOnce({
      has_lead_silence: false,
      suggested_start_frame: null,
      fps: 24,
      total_frames: 240,
      duration: 10.0,
    })
    renderDialog()
    await screen.findByText(/帧:/)

    fireEvent.click(screen.getByText('检测开头静音').closest('button')!)

    expect(await screen.findByText('未检测到开头静音')).toBeInTheDocument()
    expect(screen.queryByText(/前段静音/)).not.toBeInTheDocument()
  })

  it('确认裁剪时若前段静音帧有变化,调用 setAudioHeadMute 并通知父层刷新', async () => {
    vi.mocked(api.trimShot).mockResolvedValue({
      video_path: '/fake/video.mp4',
      last_frame_path: '/fake/last.png',
      trim_frames: 240,
      trim_end_sec: null,
      version: 2,
      fps: 24,
      total_frames: 240,
      duration: 10.0,
    })
    vi.mocked(api.detectSpeechStart).mockResolvedValueOnce({
      has_lead_silence: true,
      suggested_start_frame: 30,
      fps: 24,
      total_frames: 240,
      duration: 10.0,
    })
    const onShotUpdated = vi.fn()
    const onOpenChange = vi.fn()
    const onTrimmed = vi.fn()
    render(
      <TrimDialog
        shot={mockShot}
        projectId="proj-1"
        open={true}
        onOpenChange={onOpenChange}
        onTrimmed={onTrimmed}
        onShotUpdated={onShotUpdated}
      />,
    )
    await screen.findByText(/帧:/)

    fireEvent.click(screen.getByText('检测开头静音').closest('button')!)
    await screen.findByText(/前段静音: 前 30 帧/)

    fireEvent.click(screen.getByText('确认裁剪').closest('button')!)

    await waitFor(() => {
      expect(api.setAudioHeadMute).toHaveBeenCalledWith('proj-1', 1, 30)
    })
    await waitFor(() => {
      expect(onShotUpdated).toHaveBeenCalledWith(1, {
        audio_head_mute_frames: null,
        audio_head_mute_sec: null,
      })
    })
  })

  it('确认裁剪时若前段静音保存失败,保持对话框打开并显示错误,但裁剪结果仍应用', async () => {
    vi.mocked(api.trimShot).mockResolvedValue({
      video_path: '/fake/video.mp4',
      last_frame_path: '/fake/last.png',
      trim_frames: 240,
      trim_end_sec: null,
      version: 2,
      fps: 24,
      total_frames: 240,
      duration: 10.0,
    })
    vi.mocked(api.detectSpeechStart).mockResolvedValueOnce({
      has_lead_silence: true,
      suggested_start_frame: 30,
      fps: 24,
      total_frames: 240,
      duration: 10.0,
    })
    vi.mocked(api.setAudioHeadMute).mockRejectedValueOnce(new Error('前段静音保存失败'))
    const onOpenChange = vi.fn()
    const onTrimmed = vi.fn()
    render(
      <TrimDialog
        shot={mockShot}
        projectId="proj-1"
        open={true}
        onOpenChange={onOpenChange}
        onTrimmed={onTrimmed}
      />,
    )
    await screen.findByText(/帧:/)

    fireEvent.click(screen.getByText('检测开头静音').closest('button')!)
    await screen.findByText(/前段静音: 前 30 帧/)

    fireEvent.click(screen.getByText('确认裁剪').closest('button')!)

    expect(await screen.findByText('前段静音保存失败')).toBeInTheDocument()
    expect(onTrimmed).toHaveBeenCalled()
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
    expect(screen.getByText('裁剪视频 — Shot #1')).toBeInTheDocument()
  })
})
