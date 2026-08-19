import { describe, it, expect, vi, beforeEach } from 'vitest'
import { api } from '../api'
import type { ContentAnalysis } from '../types'

const makeAnalysis = (overrides: Partial<ContentAnalysis> = {}): ContentAnalysis => ({
  id: 'a1',
  title: 'Test Analysis',
  region_hint: null,
  status: 'completed',
  brief_json: null,
  error_message: null,
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
  samples: [],
  ...overrides,
})

describe('content analysis api', () => {
  beforeEach(() => {
    global.fetch = vi.fn(async () =>
      ({ ok: true, status: 200, json: async () => makeAnalysis() }) as any)

    // Mock localStorage
    global.localStorage = {
      getItem: vi.fn(() => 'testuser'),
      setItem: vi.fn(),
      removeItem: vi.fn(),
      clear: vi.fn(),
      length: 0,
      key: vi.fn(),
    } as any
  })

  it('createAnalysis posts multipart form with title, region_hint, and files', async () => {
    const analysis = makeAnalysis({ title: 'My Niche' })
    ;(global.fetch as any).mockResolvedValueOnce({ ok: true, status: 200, json: async () => analysis } as any)

    const file1 = new File([new Uint8Array([1, 2, 3])], 'sample1.mp4', { type: 'video/mp4' })
    const file2 = new File([new Uint8Array([4, 5, 6])], 'sample2.mp4', { type: 'video/mp4' })

    const res = await api.createAnalysis({ title: 'My Niche', regionHint: 'US', files: [file1, file2] })

    expect(res).toEqual(analysis)
    const [url, opts] = (global.fetch as any).mock.calls[0]
    expect(url).toContain('/api/analyses')
    expect(opts.method).toBe('POST')
    expect(opts.body instanceof FormData).toBe(true)
    // 身份改由会话 cookie 承载（FR-7 删掉了自称的 X-User-Name）。跨站 cookie
    // 只有显式 credentials:'include' 才会被带上——漏了就是登录后仍然 401。
    expect(opts.credentials).toBe('include')

    const form = opts.body as FormData
    expect(form.get('title')).toBe('My Niche')
    expect(form.get('region_hint')).toBe('US')
    expect(form.getAll('files')).toEqual([file1, file2])
  })

  it('createAnalysis omits region_hint when not provided', async () => {
    const file1 = new File([new Uint8Array([1])], 'sample1.mp4', { type: 'video/mp4' })
    await api.createAnalysis({ title: 'No Region', files: [file1] })

    const [, opts] = (global.fetch as any).mock.calls[0]
    const form = opts.body as FormData
    expect(form.get('region_hint')).toBeNull()
  })

  it('listAnalyses unwraps the analyses array', async () => {
    const analyses = [makeAnalysis({ id: 'a1' }), makeAnalysis({ id: 'a2' })]
    ;(global.fetch as any).mockResolvedValueOnce(
      { ok: true, status: 200, json: async () => ({ analyses, total: 2 }) } as any)

    const res = await api.listAnalyses()

    expect(res).toEqual(analyses)
    const [url, opts] = (global.fetch as any).mock.calls[0]
    expect(url).toContain('/api/analyses')
    expect(opts.method).toBe('GET')
  })

  it('getAnalysis fetches a single analysis by id', async () => {
    const analysis = makeAnalysis({ id: 'a42' })
    ;(global.fetch as any).mockResolvedValueOnce({ ok: true, status: 200, json: async () => analysis } as any)

    const res = await api.getAnalysis('a42')

    expect(res).toEqual(analysis)
    const [url, opts] = (global.fetch as any).mock.calls[0]
    expect(url).toContain('/api/analyses/a42')
    expect(opts.method).toBe('GET')
  })

  it('attachBrief posts analysis_id in body and returns project detail', async () => {
    const project = { id: 'p1', content_analysis_id: 'a1', attached_brief_json: '{}' }
    ;(global.fetch as any).mockResolvedValueOnce({ ok: true, status: 200, json: async () => project } as any)

    const res = await api.attachBrief('p1', 'a1')

    expect(res).toEqual(project)
    const [url, opts] = (global.fetch as any).mock.calls[0]
    expect(url).toContain('/api/projects/p1/attach-brief')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ analysis_id: 'a1' })
  })
})
