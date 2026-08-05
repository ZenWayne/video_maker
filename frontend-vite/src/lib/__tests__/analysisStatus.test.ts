import { describe, it, expect } from 'vitest'
import {
  ANALYSIS_STATUS_LABELS,
  ANALYSIS_STATUS_COLORS,
  SAMPLE_STATUS_LABELS,
  SAMPLE_STATUS_COLORS,
  parseBrief,
  analysisSteps,
} from '../analysisStatus'
import type { CreationBrief } from '../types'

const validBrief: CreationBrief = {
  niche_summary: '美食探店',
  sample_stats: { sample_n: 5, no_speech_pct: 0.1, sample_warning: null },
  hook_strategy: { common_hook_types: ['提问'], example_hooks: ['你知道吗？'] },
  script_structure: { pacing: '快', emotion: '惊喜', info_gap: '悬念', cta: '关注' },
  do: ['开门见山'],
  dont: ['冗长铺垫'],
  screenwriter_directives: '保持节奏紧凑',
}

describe('parseBrief', () => {
  it('returns the parsed object for valid JSON', () => {
    const result = parseBrief(JSON.stringify(validBrief))
    expect(result).toEqual(validBrief)
  })

  it('returns null for null', () => {
    expect(parseBrief(null)).toBeNull()
  })

  it('returns null for undefined', () => {
    expect(parseBrief(undefined)).toBeNull()
  })

  it('returns null for empty string', () => {
    expect(parseBrief('')).toBeNull()
  })

  it('returns null for malformed JSON', () => {
    expect(parseBrief('{not valid json')).toBeNull()
  })
})

describe('analysisSteps', () => {
  it('marks transcribing=active, analyzing/completed=pending for status=transcribing', () => {
    const steps = analysisSteps('transcribing')
    expect(steps.find((s) => s.key === 'transcribing')?.state).toBe('active')
    expect(steps.find((s) => s.key === 'analyzing')?.state).toBe('pending')
    expect(steps.find((s) => s.key === 'completed')?.state).toBe('pending')
  })

  it('marks transcribing=done, analyzing=active, completed=pending for status=analyzing', () => {
    const steps = analysisSteps('analyzing')
    expect(steps.find((s) => s.key === 'transcribing')?.state).toBe('done')
    expect(steps.find((s) => s.key === 'analyzing')?.state).toBe('active')
    expect(steps.find((s) => s.key === 'completed')?.state).toBe('pending')
  })

  it('marks all steps done except completed=active for status=completed', () => {
    const steps = analysisSteps('completed')
    expect(steps.find((s) => s.key === 'transcribing')?.state).toBe('done')
    expect(steps.find((s) => s.key === 'analyzing')?.state).toBe('done')
    expect(steps.find((s) => s.key === 'completed')?.state).toBe('active')
  })

  it('treats uploading like the pre-transcribing state (transcribing=active)', () => {
    const steps = analysisSteps('uploading')
    expect(steps.find((s) => s.key === 'transcribing')?.state).toBe('active')
    expect(steps.find((s) => s.key === 'analyzing')?.state).toBe('pending')
    expect(steps.find((s) => s.key === 'completed')?.state).toBe('pending')
  })

  it('marks all steps pending for status=failed', () => {
    const steps = analysisSteps('failed')
    expect(steps.every((s) => s.state === 'pending')).toBe(true)
  })
})

describe('status label/color maps', () => {
  const analysisStatuses = ['uploading', 'transcribing', 'analyzing', 'completed', 'failed'] as const
  const sampleStatuses = ['pending', 'transcribing', 'transcribed', 'failed'] as const

  it('ANALYSIS_STATUS_LABELS has an entry for every ContentAnalysisStatus', () => {
    analysisStatuses.forEach((s) => {
      expect(ANALYSIS_STATUS_LABELS[s]).toBeTruthy()
    })
    expect(Object.keys(ANALYSIS_STATUS_LABELS).sort()).toEqual([...analysisStatuses].sort())
  })

  it('ANALYSIS_STATUS_COLORS has an entry for every ContentAnalysisStatus', () => {
    analysisStatuses.forEach((s) => {
      expect(ANALYSIS_STATUS_COLORS[s]).toBeTruthy()
    })
    expect(Object.keys(ANALYSIS_STATUS_COLORS).sort()).toEqual([...analysisStatuses].sort())
  })

  it('SAMPLE_STATUS_LABELS has an entry for every ReferenceSampleStatus', () => {
    sampleStatuses.forEach((s) => {
      expect(SAMPLE_STATUS_LABELS[s]).toBeTruthy()
    })
    expect(Object.keys(SAMPLE_STATUS_LABELS).sort()).toEqual([...sampleStatuses].sort())
  })

  it('SAMPLE_STATUS_COLORS has an entry for every ReferenceSampleStatus', () => {
    sampleStatuses.forEach((s) => {
      expect(SAMPLE_STATUS_COLORS[s]).toBeTruthy()
    })
    expect(Object.keys(SAMPLE_STATUS_COLORS).sort()).toEqual([...sampleStatuses].sort())
  })
})
