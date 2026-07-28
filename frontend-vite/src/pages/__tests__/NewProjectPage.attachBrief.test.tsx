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

// router: NewProjectPage only uses navigate()
const navigateMock = vi.fn()
vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
}))

const {
  listAnalyses,
  attachBrief,
  createProject,
  uploadReferenceImages,
  startPipeline,
} = vi.hoisted(() => ({
  listAnalyses: vi.fn(),
  attachBrief: vi.fn(),
  createProject: vi.fn(),
  uploadReferenceImages: vi.fn(),
  startPipeline: vi.fn(),
}))
vi.mock('@/lib/api', () => ({
  api: {
    listAnalyses: (...a: unknown[]) => listAnalyses(...a),
    attachBrief: (...a: unknown[]) => attachBrief(...a),
    createProject: (...a: unknown[]) => createProject(...a),
    uploadReferenceImages: (...a: unknown[]) => uploadReferenceImages(...a),
    startPipeline: (...a: unknown[]) => startPipeline(...a),
  },
}))

import NewProjectPage from '@/pages/NewProjectPage'
import { useStore } from '@/lib/state'
import type { ContentAnalysis } from '@/lib/types'

function analysis(overrides: Partial<ContentAnalysis>): ContentAnalysis {
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

function makeFile(name = 'char.png', type = 'image/png') {
  return new File(['fake image content'], name, { type })
}

const renderPage = () => render(<NewProjectPage />)

async function fillRequiredFields(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByTestId('project-title-input'), '测试项目')
  await user.type(screen.getByTestId('project-theme-input'), '一句话主题描述')

  const fileInput = document.getElementById('file-input-character') as HTMLInputElement
  await user.upload(fileInput, makeFile())
}

beforeEach(() => {
  listAnalyses.mockReset()
  attachBrief.mockReset()
  createProject.mockReset()
  uploadReferenceImages.mockReset()
  startPipeline.mockReset()
  navigateMock.mockReset()
  useStore.setState({ toasts: [] })

  listAnalyses.mockResolvedValue([analysis({ id: 'a1', title: '美妆口播分析', status: 'completed' })])
  createProject.mockResolvedValue({ project_id: 'p1', status: 'script_generating' })
  uploadReferenceImages.mockResolvedValue(undefined)
  startPipeline.mockResolvedValue(undefined)
  attachBrief.mockResolvedValue({ id: 'p1' })
})

describe('NewProjectPage — attach brief', () => {
  it('calls attachBrief with the created project id and the selected analysis after selecting one', async () => {
    const user = userEvent.setup()
    renderPage()

    // wait for completed analyses to load into the select
    await waitFor(() => expect(listAnalyses).toHaveBeenCalled())
    const select = (await screen.findByTestId('attach-brief-select')) as HTMLSelectElement
    await waitFor(() => expect(select.querySelectorAll('option').length).toBeGreaterThan(1))

    await user.selectOptions(select, 'a1')

    await fillRequiredFields(user)
    await user.click(screen.getByTestId('create-project-submit'))

    await waitFor(() => expect(createProject).toHaveBeenCalled())
    await waitFor(() => expect(attachBrief).toHaveBeenCalledWith('p1', 'a1'))
    await waitFor(() => expect(startPipeline).toHaveBeenCalledWith('p1'))
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/projects/p1/shots'))

    // Ordering: attachBrief must complete BEFORE startPipeline is invoked, otherwise the
    // run_screenwriter worker can read the project row before attached_brief_json commits.
    expect(attachBrief.mock.invocationCallOrder[0]).toBeLessThan(
      startPipeline.mock.invocationCallOrder[0]
    )
  })

  it('does not call attachBrief when "无" (default) stays selected', async () => {
    const user = userEvent.setup()
    renderPage()

    await waitFor(() => expect(listAnalyses).toHaveBeenCalled())
    await fillRequiredFields(user)
    await user.click(screen.getByTestId('create-project-submit'))

    await waitFor(() => expect(createProject).toHaveBeenCalled())
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/projects/p1/shots'))
    expect(attachBrief).not.toHaveBeenCalled()
  })

  it('still navigates when attachBrief fails, but shows an error toast', async () => {
    const user = userEvent.setup()
    attachBrief.mockRejectedValue(new Error('简报已被删除'))
    renderPage()

    await waitFor(() => expect(listAnalyses).toHaveBeenCalled())
    const select = (await screen.findByTestId('attach-brief-select')) as HTMLSelectElement
    await waitFor(() => expect(select.querySelectorAll('option').length).toBeGreaterThan(1))
    await user.selectOptions(select, 'a1')

    await fillRequiredFields(user)
    await user.click(screen.getByTestId('create-project-submit'))

    await waitFor(() => expect(attachBrief).toHaveBeenCalledWith('p1', 'a1'))
    await waitFor(() => expect(startPipeline).toHaveBeenCalledWith('p1'))
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/projects/p1/shots'))
    await waitFor(() => {
      const toasts = useStore.getState().toasts
      expect(toasts.some((t) => t.type === 'error' && t.message.includes('简报'))).toBe(true)
    })

    // Even though attachBrief rejected, the pipeline must still be started (attach failure
    // is isolated and must not skip startPipeline or block navigation).
    expect(attachBrief.mock.invocationCallOrder[0]).toBeLessThan(
      startPipeline.mock.invocationCallOrder[0]
    )
  })
})
