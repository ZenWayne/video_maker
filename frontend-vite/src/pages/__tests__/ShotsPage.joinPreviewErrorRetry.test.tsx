// frontend-vite/src/pages/__tests__/ShotsPage.joinPreviewErrorRetry.test.tsx
//
// The 连贯性预览 (join-preview) modal's <video src={joinPreviewUrl}> shows
// api.joinPreview()'s preview_url — another to_media_url() signed URL subject
// to the same TTL. On <video onError>, refreshJoinPreview() must re-derive
// the shot ids from the REMEMBERED joinPreviewShotIds (not from whatever
// happens to be selected right now — the user may have changed their
// selection while the modal is open) and re-call api.joinPreview to mint a
// freshly-signed preview_url.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { TooltipProvider } from '@/components/ui/tooltip'

vi.hoisted(() => {
  const store = new Map<string, string>()
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
      setItem: (k: string, v: string) => void store.set(k, String(v)),
      removeItem: (k: string) => void store.delete(k),
      clear: () => store.clear(),
      key: () => null,
      length: 0,
    },
  })
})

vi.mock('react-router-dom', () => ({
  useParams: () => ({ id: 'p1' }),
  useNavigate: () => vi.fn(),
}))

vi.mock('@/components/ProgressStream', () => ({ ProgressStream: () => null }))
vi.mock('@/components/ReferenceAssetsPanel', () => ({ ReferenceAssetsPanel: () => null }))

const { getProject, joinPreview } = vi.hoisted(() => ({
  getProject: vi.fn(),
  joinPreview: vi.fn(),
}))
vi.mock('@/lib/api', () => ({
  api: {
    getProject: (...a: unknown[]) => getProject(...a),
    joinPreview: (...a: unknown[]) => joinPreview(...a),
  },
}))

import ShotsPage from '@/pages/ShotsPage'
import { useStore } from '@/lib/state'

function shot(shot_id: number, status: string) {
  return {
    id: shot_id, project_id: 'p1', shot_id, text: 't', shot_type: 'Medium Shot',
    visual_description: 'v', shot_duration: 8, status,
    align_with_previous: false, use_prev_last_frame: false, motion_prompt: 'm',
    video_path: '/v.mp4', last_frame_path: null,
    word_count_warning: false, error_message: null, custom_first_frame_path: null,
    custom_reference_paths: null, reference_image_hint: null,
    vc_status: null, vc_error_message: null, cc_status: null, cc_error_message: null,
    target_last_frame_path: null, tf_status: null, tf_error_message: null,
    tf_confirmed: false, auto_trim: true,
  }
}

function project(shots: ReturnType<typeof shot>[]) {
  return {
    id: 'p1', title: 'T', status: 'shot_review', aspect_ratio: '9:16',
    scene_overview: 'scene', shots,
    reference_images: [],
    reference_voice_shot_id: null, reference_voice_path: null,
    auto_voice_calibrate: false,
  }
}

const renderPage = () =>
  render(<TooltipProvider><ShotsPage /></TooltipProvider>)

beforeEach(() => {
  getProject.mockReset()
  joinPreview.mockReset()
  useStore.setState({ shots: [], currentProject: null, selectedShotIds: new Set() })
})

function getModalVideo(): HTMLVideoElement {
  return document.querySelector('[data-testid="join-preview-modal"] video')!
}

async function openJoinPreview() {
  getProject.mockResolvedValue(
    project([shot(1, 'completed'), shot(2, 'completed'), shot(3, 'completed')])
  )
  renderPage()
  await screen.findByTestId('shots-list')

  fireEvent.click(await screen.findByTestId('shot-select-1'))
  fireEvent.click(await screen.findByTestId('shot-select-2'))

  const btn = screen.getByTestId('join-preview-button')
  await waitFor(() => expect(btn).toBeEnabled())
  fireEvent.click(btn)

  await screen.findByTestId('join-preview-modal')
}

describe('ShotsPage join-preview video error retry (signed URL expiry)', () => {
  it('re-derives shot ids from the remembered joinPreviewShotIds and re-calls joinPreview on error', async () => {
    joinPreview.mockResolvedValueOnce({ preview_url: '/preview.mp4?sig=1' })
    await openJoinPreview()
    expect(joinPreview).toHaveBeenCalledTimes(1)
    expect(joinPreview).toHaveBeenLastCalledWith('p1', [1, 2])

    // User changes their page-level selection WHILE the modal is still open —
    // the retry must still use the ORIGINAL [1, 2], not whatever is selected now.
    fireEvent.click(screen.getByTestId('shot-select-3'))

    joinPreview.mockResolvedValueOnce({ preview_url: '/preview.mp4?sig=2' })
    fireEvent.error(getModalVideo())

    await waitFor(() => expect(joinPreview).toHaveBeenCalledTimes(2))
    expect(joinPreview).toHaveBeenLastCalledWith('p1', [1, 2])
  })

  it('updates the modal <video src> to the freshly-signed preview_url after refetch', async () => {
    joinPreview.mockResolvedValueOnce({ preview_url: '/preview.mp4?sig=1' })
    await openJoinPreview()
    expect(getModalVideo().getAttribute('src')).toBe('/preview.mp4?sig=1')

    joinPreview.mockResolvedValueOnce({ preview_url: '/preview.mp4?sig=2' })
    fireEvent.error(getModalVideo())

    await waitFor(() =>
      expect(getModalVideo().getAttribute('src')).toBe('/preview.mp4?sig=2')
    )
  })

  it('does not retry a second time for the same (still-stale) preview_url', async () => {
    joinPreview.mockResolvedValueOnce({ preview_url: '/preview.mp4?sig=1' })
    await openJoinPreview()

    // Refetch keeps returning the SAME url (object genuinely broken, or a
    // same-second re-sign collision) — only ONE retry call is allowed.
    joinPreview.mockResolvedValue({ preview_url: '/preview.mp4?sig=1' })
    fireEvent.error(getModalVideo())
    await waitFor(() => expect(joinPreview).toHaveBeenCalledTimes(2))

    fireEvent.error(getModalVideo())
    fireEvent.error(getModalVideo())
    await new Promise((r) => setTimeout(r, 0))
    expect(joinPreview).toHaveBeenCalledTimes(2)
  })
})
