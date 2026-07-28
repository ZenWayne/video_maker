import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { AnalysisProgress } from '@/components/AnalysisProgress'
import type { ContentAnalysis, ReferenceSample } from '@/lib/types'

function sample(overrides: Partial<ReferenceSample>): ReferenceSample {
  return {
    id: 1,
    analysis_id: 'a1',
    order_index: 0,
    video_path: 'viral_01.mp4',
    has_speech: true,
    hook_text: null,
    full_transcript: null,
    language: '中文',
    status: 'transcribed',
    error_message: null,
    created_at: '2026-07-01T00:00:00Z',
    ...overrides,
  }
}

function analysis(overrides: Partial<ContentAnalysis> = {}): ContentAnalysis {
  return {
    id: 'a1',
    title: '美妆口播分析',
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

describe('AnalysisProgress', () => {
  it('shows analyzing as active step, coverage text, and per-sample rows incl. greyed skip row', () => {
    const samples: ReferenceSample[] = [
      sample({ id: 1, video_path: 'viral_01.mp4', hook_text: '你绝对想不到', status: 'transcribed', has_speech: true }),
      sample({ id: 2, video_path: 'viral_02.mp4', hook_text: '开局一个碗', status: 'transcribed', has_speech: true }),
      sample({ id: 3, video_path: 'viral_03.mp4', hook_text: null, status: 'transcribed', has_speech: true }),
      sample({ id: 4, video_path: 'viral_04.mp4', hook_text: null, status: 'transcribed', has_speech: false }),
    ]

    render(<AnalysisProgress analysis={analysis({ status: 'analyzing', samples })} />)

    // stepper: active step should be 归纳 (analyzing)
    const stepper = screen.getByTestId('analysis-stepper')
    expect(within(stepper).getByText('转写口播')).toBeInTheDocument()
    expect(within(stepper).getByText('联合归纳')).toBeInTheDocument()
    expect(within(stepper).getByText('完成')).toBeInTheDocument()
    const activeLabel = within(stepper).getByText('联合归纳')
    expect(activeLabel.className).toMatch(/text-blue-700/)

    // coverage line: 3 speech samples transcribed / 4 total, no_speech_pct = 25%
    expect(screen.getByText(/已转写 3\/4 · 无人声占比 25%/)).toBeInTheDocument()

    // per-sample rows
    const row1 = screen.getByTestId('sample-row-1')
    expect(within(row1).getByText('viral_01.mp4')).toBeInTheDocument()
    expect(within(row1).getByText('你绝对想不到')).toBeInTheDocument()

    const row4 = screen.getByTestId('sample-row-4')
    expect(within(row4).getByText('viral_04.mp4')).toBeInTheDocument()
    expect(within(row4).getByText('无人声・跳过')).toBeInTheDocument()
    expect(row4.className).toMatch(/opacity-60/)
  })
})
