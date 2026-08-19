// 会话鉴权的前端契约（FR-6 / FR-7）
//
// 这几条都是「漏了就静默坏掉」的类型：漏 credentials 表现为登录后仍然 401，
// 漏 withCredentials 表现为进度流静默卡住，把 402 当 401 处理表现为余额不足时
// 莫名其妙被登出。

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { api, setUnauthorizedHandler, isInsufficientCredits, APIErrorClass } from '../api'
import { createSSEConnection } from '../sse'
import { createAnalysisSSEConnection } from '../analysisSse'

function mockResponse(status: number, body: unknown = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: String(status),
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as any
}

describe('凭据透传', () => {
  beforeEach(() => {
    global.fetch = vi.fn(async () => mockResponse(200, { items: [], total: 0 })) as any
  })
  afterEach(() => {
    setUnauthorizedHandler(null)
    vi.restoreAllMocks()
  })

  it('JSON 请求带 credentials: include', async () => {
    await api.listProjects()
    const [, opts] = (global.fetch as any).mock.calls[0]
    expect(opts.credentials).toBe('include')
  })

  it('multipart 上传同样带 credentials: include', async () => {
    ;(global.fetch as any).mockResolvedValueOnce(mockResponse(200, []))
    const file = new File([new Uint8Array([1])], 'a.png', { type: 'image/png' })
    await api.uploadReferenceImages('p1', [file], 'character')
    const [, opts] = (global.fetch as any).mock.calls[0]
    expect(opts.credentials).toBe('include')
    expect(opts.body instanceof FormData).toBe(true)
  })

  it('不再发送自称身份的 X-User-Name', async () => {
    await api.listProjects()
    const [, opts] = (global.fetch as any).mock.calls[0]
    expect(opts.headers?.['X-User-Name']).toBeUndefined()
  })
})

describe('401 与 402 的处置必须分开', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })
  afterEach(() => setUnauthorizedHandler(null))

  it('401 触发跳登录回调', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    global.fetch = vi.fn(async () => mockResponse(401, { detail: '未登录' })) as any

    await expect(api.listProjects()).rejects.toThrow()
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('402 绝不触发跳登录回调，只是一个可识别的错误', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    global.fetch = vi.fn(async () =>
      mockResponse(402, { detail: '点数不足：本次操作需要 120 点，当前余额 0 点。' })) as any

    const err = await api.startPipeline('p1').catch((e) => e)
    expect(onUnauthorized).not.toHaveBeenCalled()
    expect(isInsufficientCredits(err)).toBe(true)
    expect((err as APIErrorClass).status).toBe(402)
    expect((err as Error).message).toContain('点数不足')
  })

  it('身份探测（me）不触发跳转，否则启动即死循环', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    global.fetch = vi.fn(async () => mockResponse(401, { detail: '未登录' })) as any

    await expect(api.me()).rejects.toThrow()
    expect(onUnauthorized).not.toHaveBeenCalled()
  })
})

describe('后端是否强制校验的探测', () => {
  afterEach(() => setUnauthorizedHandler(null))

  it('受保护端点返回 200 → 没强制（未登录也能用）', async () => {
    global.fetch = vi.fn(async () => mockResponse(200, { items: [], total: 0 })) as any
    expect(await api.isAuthEnforced()).toBe(false)
  })

  it('受保护端点返回 401 → 已强制（该跳登录页了）', async () => {
    global.fetch = vi.fn(async () => mockResponse(401, { detail: '未登录' })) as any
    expect(await api.isAuthEnforced()).toBe(true)
  })

  it('探测本身不触发跳转', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    global.fetch = vi.fn(async () => mockResponse(401, {})) as any
    await api.isAuthEnforced()
    expect(onUnauthorized).not.toHaveBeenCalled()
  })
})

describe('SSE 必须带凭据', () => {
  it('两条 SSE 通道都设了 withCredentials', () => {
    const created: Array<{ url: string; init?: EventSourceInit }> = []
    class FakeEventSource {
      onmessage: ((e: MessageEvent) => void) | null = null
      onerror: ((e: Event) => void) | null = null
      constructor(url: string, init?: EventSourceInit) {
        created.push({ url, init })
      }
      close() {}
    }
    ;(global as any).EventSource = FakeEventSource

    // EventSource 不支持自定义请求头（Web 标准限制），所以进度流的凭据只能
    // 靠 cookie；漏掉 withCredentials 的表现是「进度条一直不动」而不是报错。
    createSSEConnection('p1')
    createAnalysisSSEConnection('a1')

    expect(created).toHaveLength(2)
    expect(created[0].url).toContain('/api/projects/p1/stream')
    expect(created[0].init?.withCredentials).toBe(true)
    expect(created[1].url).toContain('/api/analyses/a1/stream')
    expect(created[1].init?.withCredentials).toBe(true)
  })
})

describe('鉴权 API 形状', () => {
  beforeEach(() => {
    global.fetch = vi.fn(async () =>
      mockResponse(200, { username: 'alice', credits: 500, is_admin: false })) as any
  })

  it('login/register 打的是正确端点且带凭据（Set-Cookie 才会被接受）', async () => {
    await api.login('alice', 'pw')
    let [url, opts] = (global.fetch as any).mock.calls[0]
    expect(url).toContain('/api/auth/login')
    expect(opts.credentials).toBe('include')
    expect(JSON.parse(opts.body)).toEqual({ username: 'alice', password: 'pw' })

    await api.register('bob', 'pw12345678')
    ;[url, opts] = (global.fetch as any).mock.calls[1]
    expect(url).toContain('/api/auth/register')
    expect(opts.credentials).toBe('include')
  })

  it('me 返回用户名与余额', async () => {
    const me = await api.me()
    expect(me).toEqual({ username: 'alice', credits: 500, is_admin: false })
  })
})
