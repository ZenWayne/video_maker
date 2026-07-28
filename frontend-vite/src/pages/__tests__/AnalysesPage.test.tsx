import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

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

const { listAnalyses } = vi.hoisted(() => ({ listAnalyses: vi.fn() }))
vi.mock('@/lib/api', () => ({
  api: {
    listAnalyses: (...a: unknown[]) => listAnalyses(...a),
  },
}))

import AnalysesPage from '@/pages/AnalysesPage'
import type { ContentAnalysis } from '@/lib/types'

function analysis(overrides: Partial<ContentAnalysis>): ContentAnalysis {
  return {
    id: 'a1',
    title: 'Untitled',
    region_hint: null,
    status: 'analyzing',
    brief_json: null,
    error_message: null,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    samples: [],
    ...overrides,
  }
}

const renderPage = () =>
  render(
    <MemoryRouter>
      <AnalysesPage />
    </MemoryRouter>
  )

beforeEach(() => {
  listAnalyses.mockReset()
})

describe('AnalysesPage', () => {
  it('renders analysis cards with title, status badge, and sample count', async () => {
    listAnalyses.mockResolvedValue([
      analysis({
        id: 'a1',
        title: '美妆口播分析',
        status: 'analyzing',
        samples: [
          { id: 1, analysis_id: 'a1', order_index: 0, video_path: 'v1.mp4', has_speech: true, hook_text: null, full_transcript: null, language: null, status: 'transcribed', error_message: null, created_at: '2026-07-01T00:00:00Z' },
          { id: 2, analysis_id: 'a1', order_index: 1, video_path: 'v2.mp4', has_speech: true, hook_text: null, full_transcript: null, language: null, status: 'transcribed', error_message: null, created_at: '2026-07-01T00:00:00Z' },
        ],
      }),
      analysis({
        id: 'a2',
        title: '美食探店分析',
        status: 'completed',
        brief_json: JSON.stringify({
          niche_summary: '美食探店赛道以真实体验和强烈视觉冲击取胜。',
          sample_stats: { sample_n: 3, no_speech_pct: 0, sample_warning: null },
          hook_strategy: { common_hook_types: [], example_hooks: [] },
          script_structure: { pacing: '', emotion: '', info_gap: '', cta: '' },
          do: [],
          dont: [],
          screenwriter_directives: '',
        }),
        samples: [
          { id: 3, analysis_id: 'a2', order_index: 0, video_path: 'v3.mp4', has_speech: true, hook_text: null, full_transcript: null, language: null, status: 'transcribed', error_message: null, created_at: '2026-07-01T00:00:00Z' },
        ],
      }),
    ])

    renderPage()

    // wait for both cards to render
    const card1 = await screen.findByTestId('analysis-card-a1')
    const card2 = await screen.findByTestId('analysis-card-a2')

    // titles
    expect(within(card1).getByText('美妆口播分析')).toBeInTheDocument()
    expect(within(card2).getByText('美食探店分析')).toBeInTheDocument()

    // status badge labels (scoped to card — the filter <select> also has these option labels)
    expect(within(card1).getByText('归纳中')).toBeInTheDocument()
    expect(within(card2).getByText('已完成')).toBeInTheDocument()

    // sample counts
    expect(within(card1).getByText(/2 个样本/)).toBeInTheDocument()
    expect(within(card2).getByText(/1 个样本/)).toBeInTheDocument()

    // completed card shows niche_summary
    expect(
      within(card2).getByText('美食探店赛道以真实体验和强烈视觉冲击取胜。')
    ).toBeInTheDocument()

    await waitFor(() => expect(listAnalyses).toHaveBeenCalled())
  })
})
