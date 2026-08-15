// lib/auth.tsx - 会话身份上下文
//
// 凭据是 httpOnly cookie，JS 读不到（这正是它防得住 XSS 的原因），所以前端
// 无法自己判断「有没有登录」——只能问后端。启动时做两件事：
//   1. GET /api/auth/me —— 有会话就拿到用户名和余额
//   2. 没会话时探一下后端是否已开启强制校验（AUTH_ENFORCED）
//
// 第 2 步决定了门禁的强弱：分期上线期间开关是关的，未登录也能正常用，这时
// 把人挡在登录页会平白改掉现有用户的工作流。等 P3 打开开关，同一份前端代码
// 自动变成「必须登录」，不需要再发一次版。

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { useNavigate } from 'react-router-dom'
import { api, setUnauthorizedHandler } from './api'
import type { CurrentUser } from './types'

interface AuthState {
  user: CurrentUser | null
  /** 后端是否强制校验。false 时未登录也能用（AUTH_ENFORCED=false）。 */
  enforced: boolean
  /** 启动探测是否还在进行——决定要不要先渲染骨架而不是闪一下登录页。 */
  loading: boolean
  login: (username: string, password: string) => Promise<void>
  register: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  /** 触发计费操作后刷新余额。 */
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate()
  const [user, setUser] = useState<CurrentUser | null>(null)
  const [enforced, setEnforced] = useState(false)
  const [loading, setLoading] = useState(true)
  // 会话过期时可能有多个请求同时 401，只跳一次。
  const redirecting = useRef(false)

  const refresh = useCallback(async () => {
    try {
      setUser(await api.me())
    } catch {
      setUser(null)
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const me = await api.me()
        if (!cancelled) {
          setUser(me)
          // 能拿到身份就说明会话有效；强制与否此时不影响可用性，
          // 但仍要知道，登出后才能决定是跳登录页还是留在匿名态。
          setEnforced(await api.isAuthEnforced())
        }
      } catch {
        if (!cancelled) {
          setUser(null)
          setEnforced(await api.isAuthEnforced())
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    setUnauthorizedHandler(() => {
      if (redirecting.current) return
      redirecting.current = true
      setUser(null)
      navigate('/login', { replace: true })
      // 允许下一次会话过期再次跳转
      window.setTimeout(() => {
        redirecting.current = false
      }, 1000)
    })
    return () => setUnauthorizedHandler(null)
  }, [navigate])

  const login = useCallback(async (username: string, password: string) => {
    setUser(await api.login(username, password))
  }, [])

  const register = useCallback(async (username: string, password: string) => {
    setUser(await api.register(username, password))
  }, [])

  const logout = useCallback(async () => {
    try {
      await api.logout()
    } finally {
      setUser(null)
      navigate('/login', { replace: true })
    }
  }, [navigate])

  const value = useMemo<AuthState>(
    () => ({ user, enforced, loading, login, register, logout, refresh }),
    [user, enforced, loading, login, register, logout, refresh],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within <AuthProvider>')
  return ctx
}
