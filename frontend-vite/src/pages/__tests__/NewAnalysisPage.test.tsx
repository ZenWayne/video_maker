import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

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

// router: NewAnalysisPage only uses navigate()
const navigateMock = vi.fn()
vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
}))

const { createAnalysis } = vi.hoisted(() => ({ createAnalysis: vi.fn() }))
vi.mock('@/lib/api', () => ({
  api: {
    createAnalysis: (...a: unknown[]) => createAnalysis(...a),
  },
}))

import NewAnalysisPage from '@/pages/NewAnalysisPage'
import { useStore } from '@/lib/state'

function makeFile(name = 'sample.mp4', type = 'video/mp4') {
  return new File(['fake video content'], name, { type })
}

const renderPage = () => render(<NewAnalysisPage />)

beforeEach(() => {
  createAnalysis.mockReset()
  navigateMock.mockReset()
  useStore.setState({ toasts: [] })
})

describe('NewAnalysisPage', () => {
  it('submits title + region hint + video files, then navigates to the new analysis', async () => {
    const user = userEvent.setup()
    createAnalysis.mockResolvedValue({
      id: 'a1',
      title: '美妆口播分析',
      region_hint: 'zh',
      status: 'uploading',
      brief_json: null,
      error_message: null,
      created_at: '2026-07-01T00:00:00Z',
      updated_at: '2026-07-01T00:00:00Z',
      samples: [],
    })

    renderPage()

    await user.type(screen.getByTestId('analysis-title-input'), '美妆口播分析')
    await user.type(screen.getByTestId('analysis-lang-input'), 'zh')

    const fileInput = screen
      .getByTestId('analysis-upload')
      .querySelector('input[type="file"]') as HTMLInputElement
    const file = makeFile()
    await user.upload(fileInput, file)

    await user.click(screen.getByTestId('create-analysis-submit'))

    await waitFor(() => expect(createAnalysis).toHaveBeenCalled())
    expect(createAnalysis).toHaveBeenCalledWith({
      title: '美妆口播分析',
      regionHint: 'zh',
      files: [file],
    })

    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/analyses/a1'))
  })

  it('shows an error toast when the backend rejects the region hint (400)', async () => {
    const user = userEvent.setup()
    class APIErrorLike extends Error {
      code = 'INVALID_REGION_HINT'
    }
    createAnalysis.mockRejectedValue(new APIErrorLike('非模型支持的语言码（如 en-US、美国）会被拒绝'))

    renderPage()

    await user.type(screen.getByTestId('analysis-title-input'), '测试分析')
    await user.type(screen.getByTestId('analysis-lang-input'), 'en-US')

    const fileInput = screen
      .getByTestId('analysis-upload')
      .querySelector('input[type="file"]') as HTMLInputElement
    await user.upload(fileInput, makeFile())

    await user.click(screen.getByTestId('create-analysis-submit'))

    await waitFor(() => expect(createAnalysis).toHaveBeenCalled())
    await waitFor(() => {
      const toasts = useStore.getState().toasts
      expect(toasts.some((t) => t.type === 'error' && t.message.includes('语言码'))).toBe(true)
    })
    expect(navigateMock).not.toHaveBeenCalled()
  })
})
