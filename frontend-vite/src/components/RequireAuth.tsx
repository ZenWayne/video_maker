// components/RequireAuth.tsx - 路由门禁 + 余额提示
//
// 门禁强度**跟随后端**：只有后端开了强制校验（AUTH_ENFORCED）才把未登录的人
// 挡到登录页。分期上线期间开关是关的，未登录照常可用——这时硬挡会平白改掉
// 现有用户的工作流，而 P3 打开开关后同一份代码自动收紧，不用再发版。

import { useEffect, useRef, type ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { AlertTriangle } from 'lucide-react'
import { useAuth } from '@/lib/auth'

export function RequireAuth({ children }: { children: ReactNode }) {
  const { user, enforced, loading, refresh } = useAuth()
  const location = useLocation()
  const refreshed = useRef(false)

  // 每次进入受保护页面刷一次余额：计费发生在后端，前端不刷就会一直显示旧数字。
  // 每条路由各自包了一层 RequireAuth，所以换页即重新挂载，等于换页刷新——
  // 不需要轮询。
  useEffect(() => {
    if (user && !refreshed.current) {
      refreshed.current = true
      void refresh()
    }
  }, [user, refresh])

  // 探测未完成前先别渲染：直接放行会让受保护页面闪一下再跳走，
  // 直接跳走则会把本来有会话的用户误踢到登录页。
  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center text-sm text-zinc-500">
        加载中…
      </div>
    )
  }

  if (enforced && !user) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  }

  return (
    <>
      {user && user.credits <= 0 && (
        // 「触发前就提示」而不是等后端 402：余额为 0 时**所有**生成类操作都会
        // 被拒，先说清楚比让用户点一次再吃一个错误好。
        <div
          data-testid="zero-credit-banner"
          className="flex items-center gap-2 px-4 py-2 bg-amber-50 border-b border-amber-200 text-sm text-amber-900"
        >
          <AlertTriangle className="w-4 h-4 shrink-0" />
          点数余额为 0：生成剧本、分镜视频、首尾帧等操作都会被拒绝。请联系管理员发放点数。
        </div>
      )}
      {children}
    </>
  )
}
