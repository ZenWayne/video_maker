// pages/LoginPage.tsx - 登录 / 注册
//
// 一个组件两条路由（/login、/register）：两者的表单、校验、错误处置完全一样，
// 拆成两个文件只会让两边慢慢长歪。

import { useState, type FormEvent } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { useAuth } from '@/lib/auth'
import { APIErrorClass } from '@/lib/api'

export default function LoginPage({ mode = 'login' }: { mode?: 'login' | 'register' }) {
  const navigate = useNavigate()
  const location = useLocation()
  const { login, register } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const isRegister = mode === 'register'
  // 被 401 踢过来时记下原本要去的地方，登录后送回去。
  const from = (location.state as { from?: string } | null)?.from ?? '/'

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      if (isRegister) {
        await register(username.trim().toLowerCase(), password)
      } else {
        await login(username.trim().toLowerCase(), password)
      }
      navigate(from, { replace: true })
    } catch (err) {
      if (err instanceof APIErrorClass && err.status === 429) {
        setError('注册过于频繁：同一网络出口每小时只能注册一次，请稍后再试。')
      } else if (err instanceof APIErrorClass) {
        setError(err.message)
      } else {
        setError('网络错误，请稍后再试')
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="min-h-screen bg-zinc-50 flex items-center justify-center px-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <h1 className="text-lg font-semibold text-zinc-900">
            {isRegister ? '注册' : '登录'}
          </h1>
          <p className="text-sm text-zinc-500">
            {isRegister
              ? '注册后即可登录浏览界面；点数需由管理员发放后才能使用生成功能。'
              : '视频制作工具'}
          </p>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="username">用户名</Label>
              <Input
                id="username"
                data-testid="username-input"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                autoFocus
                required
              />
              {isRegister && (
                <p className="text-xs text-zinc-500">
                  3–32 位，小写字母、数字、点、下划线、连字符
                </p>
              )}
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="password">密码</Label>
              <Input
                id="password"
                data-testid="password-input"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete={isRegister ? 'new-password' : 'current-password'}
                required
              />
              {isRegister && <p className="text-xs text-zinc-500">至少 8 位</p>}
            </div>

            {error && (
              <p data-testid="auth-error" className="text-sm text-red-600">
                {error}
              </p>
            )}

            <Button
              type="submit"
              data-testid="auth-submit"
              className="w-full"
              disabled={submitting || !username || !password}
            >
              {submitting ? '提交中…' : isRegister ? '注册' : '登录'}
            </Button>
          </form>

          <p className="mt-4 text-sm text-zinc-500 text-center">
            {isRegister ? (
              <>
                已有账号？
                <Link to="/login" className="text-zinc-900 underline ml-1">
                  去登录
                </Link>
              </>
            ) : (
              <>
                还没有账号？
                <Link to="/register" className="text-zinc-900 underline ml-1">
                  去注册
                </Link>
              </>
            )}
          </p>
        </CardContent>
      </Card>
    </div>
  )
}
