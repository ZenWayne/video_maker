import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { TooltipProvider } from '@/components/ui/tooltip'

// --- mocks --------------------------------------------------------------
// state.ts touches localStorage at import time; node's experimental localStorage
// shadows jsdom's and throws. Install a working stub before any import runs.
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

// router: ShotsPage reads :id and uses navigate()
vi.mock('react-router-dom', () => ({
  useParams: () => ({ id: 'p1' }),
  useNavigate: () => vi.fn(),
}))

// ProgressStream opens a streaming fetch on mount — irrelevant here, stub it.
vi.mock('@/components/ProgressStream', () => ({ ProgressStream: () => null }))
// ReferenceAssetsPanel renders media thumbnails — not under test, stub it.
vi.mock('@/components/ReferenceAssetsPanel', () => ({ ReferenceAssetsPanel: () => null }))

const { getProject } = vi.hoisted(() => ({ getProject: vi.fn() }))
vi.mock('@/lib/api', () => ({
  api: {
    // only getProject runs during render (mount effect); the rest fire on click.
    getProject: (...a: unknown[]) => getProject(...a),
  },
}))

import ShotsPage from '@/pages/ShotsPage'
import { useStore } from '@/lib/state'

function shot(shot_id: number, status: string) {
  return {
    id: shot_id, project_id: 'p1', shot_id, text: 't', shot_type: 'Medium Shot',
    visual_description: 'v', shot_duration: 8, status,
    align_with_previous: false, use_prev_last_frame: false, motion_prompt: 'm',
    video_path: null, last_frame_path: null,
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
    reference_images: [{ id: 'r1', kind: 'character', filename: 'c.jpg' }],
    reference_voice_shot_id: null, reference_voice_path: null,
    auto_voice_calibrate: false,
  }
}

const renderPage = () =>
  render(<TooltipProvider><ShotsPage /></TooltipProvider>)

beforeEach(() => {
  getProject.mockReset()
  useStore.setState({ shots: [], currentProject: null, selectedShotIds: new Set() })
})

describe('ShotsPage — generate action availability in shot_review', () => {
  // The status problem: a project can enter shot_review with ZERO completed shots
  // (e.g. cancel generation before shot 1 finishes). There must still be an enabled
  // page-level button to (re)start generation — otherwise the user is stranded.
  it('keeps the continue/start-generation button ENABLED when no shot is completed yet', async () => {
    getProject.mockResolvedValue(project([shot(1, 'pending'), shot(2, 'pending'), shot(3, 'pending'), shot(4, 'pending')]))
    renderPage()
    const btn = await screen.findByTestId('continue-generation-button')
    expect(btn).toBeEnabled() // regressed when disabled by `completedCount === 0`
  })

  // Regression guard for the normal mid-generation state (this is the screenshot case).
  it('enables the continue button when some shots are completed and some pending', async () => {
    getProject.mockResolvedValue(project([shot(1, 'completed'), shot(2, 'pending'), shot(3, 'pending'), shot(4, 'pending')]))
    renderPage()
    const btn = await screen.findByTestId('continue-generation-button')
    expect(btn).toBeEnabled()
  })

  // When everything is done, the action flips to export — no continue button.
  it('shows the export button (no continue) when all shots are completed', async () => {
    getProject.mockResolvedValue(project([shot(1, 'completed'), shot(2, 'completed')]))
    renderPage()
    expect(await screen.findByTestId('export-button')).toBeInTheDocument()
    expect(screen.queryByTestId('continue-generation-button')).not.toBeInTheDocument()
  })
})
