import { describe, it, expect, vi, beforeEach } from 'vitest'
import { api } from '../api'

describe('uploadForm error parsing', () => {
  beforeEach(() => {
    global.localStorage = {
      getItem: vi.fn(() => 'testuser'),
      setItem: vi.fn(),
      removeItem: vi.fn(),
      clear: vi.fn(),
      length: 0,
      key: vi.fn(),
    } as any
  })

  it('extracts the clean detail message from a JSON {detail} error body', async () => {
    global.fetch = vi.fn(async () =>
      ({
        ok: false,
        status: 400,
        text: async () => JSON.stringify({ detail: "region_hint 不是 ASR 模型支持的语言代码：'en-US'。" }),
      }) as any)

    const file1 = new File([new Uint8Array([1, 2, 3])], 'sample1.mp4', { type: 'video/mp4' })

    await expect(
      api.createAnalysis({ title: 'Bad Region', regionHint: 'en-US', files: [file1] })
    ).rejects.toThrow("region_hint 不是 ASR 模型支持的语言代码：'en-US'。")
  })

  it('falls back to the raw text when the error body is not JSON', async () => {
    global.fetch = vi.fn(async () =>
      ({
        ok: false,
        status: 500,
        text: async () => 'Internal Server Error (plain text, not JSON)',
      }) as any)

    const file1 = new File([new Uint8Array([1, 2, 3])], 'sample1.mp4', { type: 'video/mp4' })

    await expect(
      api.createAnalysis({ title: 'Server Error', files: [file1] })
    ).rejects.toThrow('Internal Server Error (plain text, not JSON)')
  })
})
