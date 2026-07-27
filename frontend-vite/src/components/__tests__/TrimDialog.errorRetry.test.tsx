// frontend-vite/src/components/__tests__/TrimDialog.errorRetry.test.tsx
//
// TrimDialog's preview <video> shows sourceVideoUrl (api.getVideoInfo()'s
// source_video_url), a signed COS URL subject to the same TTL as everything
// else to_media_url() signs. On <video onError>, refreshSourceUrl() must
// re-fetch getVideoInfo() and update ONLY sourceVideoUrl — deliberately not
// touching fps/totalFrames/endFrame/headMuteFrame/peaks, which may reflect
// in-progress user edits.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { TrimDialog } from '../TrimDialog'
import { api } from '@/lib/api'
import type { Shot } from '@/lib/types'

vi.mock('@/lib/api', () => ({
  api: {
    getVideoInfo: vi.fn().mockResolvedValue({
      fps: 24,
      total_frames: 240,
      duration: 10.0,
      has_backup: false,
      speech_end_frame: 180,
      speech_end_sec: 7.5,
      source_video_url: '/signed/source.mp4?sig=1',
    }),
    getWaveform: vi.fn().mockResolvedValue({ peaks: [0.2, 0.6, 0.4, 0.8, 0.3] }),
    getFilmstrip: vi.fn().mockResolvedValue({ url: '/strip.png', count: 12, cell_aspect: 16 / 9 }),
    trimShot: vi.fn(),
    detectSpeechStart: vi.fn(),
    setAudioHeadMute: vi.fn(),
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
  video_path: '/fallback/shot-video-path.mp4',
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

beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
    clearRect: vi.fn(), fillRect: vi.fn(), fillStyle: '',
  } as unknown as CanvasRenderingContext2D)
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    left: 0, width: 240, right: 240, top: 0, bottom: 0, height: 0, x: 0, y: 0, toJSON: () => ({}),
  } as DOMRect)
  // Call counts matter in this file (asserting exactly N getVideoInfo calls) —
  // clear accumulated history between tests without disturbing any
  // mockResolvedValueOnce queued by the test itself (queued after this runs).
  vi.mocked(api.getVideoInfo).mockClear()
})

afterEach(() => {
  vi.restoreAllMocks()
})

function getVideo(): HTMLVideoElement {
  return document.querySelector('video')!
}

/** 拖裁剪线到指定帧 —— 容器测宽 240px、totalFrames 恒为 240，故 clientX 与帧号一一对应
 *  （与 TrimDialog.test.tsx 的 trimTo() 一致：cut-line 靠 pointerDown 响应，不是 click） */
function trimTo(frame: number) {
  fireEvent.pointerDown(screen.getByTestId('cut-line'), { clientX: frame })
}

function renderDialog(shotOverride?: Shot) {
  render(
    <TrimDialog
      shot={shotOverride ?? mockShot}
      projectId="proj-1"
      open={true}
      onOpenChange={vi.fn()}
      onTrimmed={vi.fn()}
    />
  )
}

describe('TrimDialog video error retry (signed URL expiry)', () => {
  it('re-fetches getVideoInfo when the preview <video> errors', async () => {
    renderDialog()
    await screen.findByText(/帧:/)
    expect(api.getVideoInfo).toHaveBeenCalledTimes(1)

    fireEvent.error(getVideo())

    await waitFor(() => expect(api.getVideoInfo).toHaveBeenCalledTimes(2))
    expect(api.getVideoInfo).toHaveBeenLastCalledWith('proj-1', 1)
  })

  it('updates the <video src> to the freshly-signed source_video_url after refetch', async () => {
    vi.mocked(api.getVideoInfo).mockResolvedValueOnce({
      fps: 24, total_frames: 240, duration: 10.0, has_backup: false,
      speech_end_frame: 180, speech_end_sec: 7.5,
      source_video_url: '/signed/source.mp4?sig=1',
    }).mockResolvedValueOnce({
      fps: 24, total_frames: 240, duration: 10.0, has_backup: false,
      speech_end_frame: 180, speech_end_sec: 7.5,
      source_video_url: '/signed/source.mp4?sig=2',
    })
    renderDialog()
    await screen.findByText(/帧:/)
    expect(getVideo().getAttribute('src')).toBe('/signed/source.mp4?sig=1')

    fireEvent.error(getVideo())

    await waitFor(() =>
      expect(getVideo().getAttribute('src')).toBe('/signed/source.mp4?sig=2')
    )
  })

  it('does NOT reset endFrame/headMuteFrame (in-progress edit state) on refetch — only the URL', async () => {
    vi.mocked(api.detectSpeechStart).mockResolvedValueOnce({
      has_lead_silence: true, suggested_start_frame: 30,
      fps: 24, total_frames: 240, duration: 10.0,
    })
    vi.mocked(api.getVideoInfo).mockResolvedValueOnce({
      fps: 24, total_frames: 240, duration: 10.0, has_backup: false,
      speech_end_frame: 180, speech_end_sec: 7.5,
      source_video_url: '/signed/source.mp4?sig=1',
    }).mockResolvedValueOnce({
      // Refetch response deliberately differs on unrelated fields — must NOT
      // clobber the user's in-progress edits (endFrame/headMuteFrame).
      fps: 24, total_frames: 240, duration: 10.0, has_backup: true,
      speech_end_frame: 999, speech_end_sec: 40,
      source_video_url: '/signed/source.mp4?sig=2',
    })
    renderDialog()
    await screen.findByText(/帧:/)

    // User is mid-edit: trims to frame 200, and sets a head-mute point.
    trimTo(200)
    fireEvent.click(screen.getByText('检测开头静音').closest('button')!)
    await screen.findByText(/前段静音: 前 30 帧/)
    expect(screen.getByText(/帧: 200 \/ 240/)).toBeInTheDocument()

    fireEvent.error(getVideo())
    await waitFor(() =>
      expect(getVideo().getAttribute('src')).toBe('/signed/source.mp4?sig=2')
    )

    // In-progress edit state survives the refetch untouched.
    expect(screen.getByText(/帧: 200 \/ 240/)).toBeInTheDocument()
    expect(screen.getByText(/前段静音: 前 30 帧/)).toBeInTheDocument()
    // And the refetch's (unrelated) speech_end_frame/has_backup did NOT leak in either.
    expect(screen.queryByText(/静音参考: 第 999 帧/)).not.toBeInTheDocument()
  })

  it('does not retry a second time for the same (still-stale) sourceVideoUrl', async () => {
    renderDialog()
    await screen.findByText(/帧:/)
    expect(api.getVideoInfo).toHaveBeenCalledTimes(1)

    // Mocked refetch keeps returning the SAME url (as if the real backend
    // re-signed within the same second, or the object is genuinely broken).
    fireEvent.error(getVideo())
    await waitFor(() => expect(api.getVideoInfo).toHaveBeenCalledTimes(2))

    fireEvent.error(getVideo())
    fireEvent.error(getVideo())
    // No further calls — the url never changed, so the retry budget for it
    // stays spent.
    await new Promise((r) => setTimeout(r, 0))
    expect(api.getVideoInfo).toHaveBeenCalledTimes(2)
  })

  it('falls back to shot.video_path when source_video_url is null, and retry targets that same protected <video src>', async () => {
    vi.mocked(api.getVideoInfo).mockResolvedValueOnce({
      fps: 24, total_frames: 240, duration: 10.0, has_backup: false,
      speech_end_frame: 180, speech_end_sec: 7.5,
      source_video_url: null,
    }).mockResolvedValueOnce({
      fps: 24, total_frames: 240, duration: 10.0, has_backup: false,
      speech_end_frame: 180, speech_end_sec: 7.5,
      source_video_url: '/signed/source.mp4?sig=recovered',
    })
    renderDialog()
    await screen.findByText(/帧:/)

    // sourceVideoUrl is null → falls back to shot.video_path, exactly what's rendered.
    expect(getVideo().getAttribute('src')).toBe('/fallback/shot-video-path.mp4')

    fireEvent.error(getVideo())

    // Retry keys off the SAME value that was actually rendered (the fallback),
    // and once the refetch resolves a real source_video_url, the <video src>
    // switches to it.
    await waitFor(() =>
      expect(getVideo().getAttribute('src')).toBe('/signed/source.mp4?sig=recovered')
    )
  })
})
