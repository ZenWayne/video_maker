import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

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

const navigateMock = vi.fn()
vi.mock('react-router-dom', () => ({
  useParams: () => ({ id: 'a1' }),
  useNavigate: () => navigateMock,
}))

const { getAnalysis } = vi.hoisted(() => ({ getAnalysis: vi.fn() }))
vi.mock('@/lib/api', () => ({
  api: {
    getAnalysis: (...a: unknown[]) => getAnalysis(...a),
  },
}))

// Fake SSE connection — AnalysisDetailPage subscribes to 'state_snapshot' and
// 'analysis_progress' and calls close() on unmount. We never emit any events
// in these tests, so the initial fetch result is what drives rendering.
const { createAnalysisSSEConnection } = vi.hoisted(() => ({
  createAnalysisSSEConnection: vi.fn(() => ({
    subscribe: vi.fn(() => vi.fn()),
    close: vi.fn(),
  })),
}))
vi.mock('@/lib/analysisSse', () => ({
  createAnalysisSSEConnection: (...a: unknown[]) => createAnalysisSSEConnection(...a),
}))

import AnalysisDetailPage from '@/pages/AnalysisDetailPage'
import { useStore } from '@/lib/state'
import type { ContentAnalysis } from '@/lib/types'

function analysis(overrides: Partial<ContentAnalysis> = {}): ContentAnalysis {
  return {
    id: 'a1',
    title: '美妆口播分析',
    region_hint: null,
    status: 'completed',
    brief_json: null,
    error_message: null,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    samples: [],
    ...overrides,
  }
}

const renderPage = () => render(<AnalysisDetailPage />)

beforeEach(() => {
  getAnalysis.mockReset()
  navigateMock.mockReset()
  createAnalysisSSEConnection.mockClear()
  useStore.setState({ toasts: [] })
})

describe('AnalysisDetailPage', () => {
  it('shows a parse-failure state (not an all-green stepper) when completed with null brief_json', async () => {
    getAnalysis.mockResolvedValue(analysis({ status: 'completed', brief_json: null }))

    renderPage()

    const errorBox = await screen.findByTestId('brief-parse-error')
    expect(errorBox).toBeInTheDocument()
    expect(screen.getByText('简报解析失败')).toBeInTheDocument()

    // must NOT render the progress stepper (which would show all steps done)
    expect(screen.queryByTestId('analysis-stepper')).not.toBeInTheDocument()
  })

  it('shows a parse-failure state with error_message when completed with malformed brief_json', async () => {
    getAnalysis.mockResolvedValue(
      analysis({ status: 'completed', brief_json: '{not valid json', error_message: '简报生成异常' })
    )

    renderPage()

    const errorBox = await screen.findByTestId('brief-parse-error')
    expect(errorBox).toBeInTheDocument()
    expect(screen.getByText('简报生成异常')).toBeInTheDocument()
    expect(screen.queryByTestId('analysis-stepper')).not.toBeInTheDocument()
  })

  it('renders BriefView when completed with a valid brief_json', async () => {
    getAnalysis.mockResolvedValue(
      analysis({
        status: 'completed',
        brief_json: JSON.stringify({
          niche_summary: '摘要',
          sample_stats: { sample_n: 3, no_speech_pct: 0.25, sample_warning: null },
          hook_strategy: { common_hook_types: [], example_hooks: [] },
          script_structure: { pacing: '', emotion: '', info_gap: '', cta: '' },
          do: [],
          dont: [],
          screenwriter_directives: '',
        }),
      })
    )

    renderPage()

    expect(await screen.findByTestId('brief-view')).toBeInTheDocument()
    expect(screen.queryByTestId('brief-parse-error')).not.toBeInTheDocument()
    expect(screen.queryByTestId('analysis-stepper')).not.toBeInTheDocument()
  })

  it('renders AnalysisProgress (stepper) for a non-completed status', async () => {
    getAnalysis.mockResolvedValue(analysis({ status: 'analyzing', brief_json: null }))

    renderPage()

    expect(await screen.findByTestId('analysis-stepper')).toBeInTheDocument()
    expect(screen.queryByTestId('brief-parse-error')).not.toBeInTheDocument()
  })

  it('ignores a stale initial fetch response after unmount (no setState-after-unmount warning)', async () => {
    let resolveFetch: (a: ContentAnalysis) => void = () => {}
    getAnalysis.mockImplementation(
      () =>
        new Promise<ContentAnalysis>((resolve) => {
          resolveFetch = resolve
        })
    )

    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { unmount } = renderPage()
    unmount()

    // resolve after unmount — the cancelled guard must prevent setState
    resolveFetch(analysis({ status: 'completed', brief_json: null }))
    await waitFor(() => expect(getAnalysis).toHaveBeenCalled())

    // no React "state update on unmounted component" warning
    const badWarning = errorSpy.mock.calls.some((args) =>
      String(args[0]).includes('unmounted')
    )
    expect(badWarning).toBe(false)
    errorSpy.mockRestore()
  })
})
