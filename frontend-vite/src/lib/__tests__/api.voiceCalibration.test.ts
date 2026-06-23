import { describe, it, expect, vi, beforeEach } from 'vitest'
import { api } from '../api'

describe('voice calibration api', () => {
  beforeEach(() => {
    global.fetch = vi.fn(async () =>
      ({ ok: true, status: 200, json: async () => ({ auto_voice_calibrate: true }) }) as any)

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

  it('setAutoVoiceCalibrate posts enabled flag', async () => {
    const res = await api.setAutoVoiceCalibrate('p1', true)
    expect(res.auto_voice_calibrate).toBe(true)
    const [, opts] = (global.fetch as any).mock.calls[0]
    expect(JSON.parse(opts.body)).toEqual({ enabled: true })
  })

  it('uploadReferenceVoice posts multipart form data', async () => {
    ;(global.fetch as any).mockResolvedValueOnce(
      { ok: true, status: 200, json: async () => ({ reference_voice_path: '/m/p.wav', reference_voice_shot_id: null }) } as any)
    const file = new File([new Uint8Array([1, 2, 3])], 'base.wav', { type: 'audio/wav' })
    const res = await api.uploadReferenceVoice('p1', file)
    expect(res.reference_voice_path).toBe('/m/p.wav')
    const [url, opts] = (global.fetch as any).mock.calls[0]
    expect(url).toContain('/api/projects/p1/reference-voice/upload')
    expect(opts.body instanceof FormData).toBe(true)
  })
})
