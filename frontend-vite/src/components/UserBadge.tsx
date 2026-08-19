// components/UserBadge.tsx - 当前用户 + 余额 + 登出
//
// 原来这里是个「改本地用户名」的输入框：那个名字只存 localStorage、随请求头
// 自称给后端，谁都能自称任何人。身份改由会话决定之后（FR-7），可改的输入框
// 必须去掉——留着只会让人以为改个名字就换了身份。

'use client'

import { Link } from 'react-router-dom'
import { Coins, LogOut, User } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/lib/auth'

export function UserBadge() {
  const { user, logout } = useAuth()

  if (!user) {
    // 未登录（只可能在 AUTH_ENFORCED=false 时出现）——给入口，不强制。
    return (
      <Link to="/login" data-testid="login-link">
        <Button variant="ghost" size="sm">
          <User className="w-4 h-4 mr-2" />
          登录
        </Button>
      </Link>
    )
  }

  return (
    <div className="flex items-center gap-2">
      <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-zinc-100">
        <User className="w-4 h-4 text-zinc-500" />
        <span data-testid="current-username" className="text-sm text-zinc-700">
          {user.username}
        </span>
        <span
          data-testid="credit-balance"
          title="点数余额"
          className={`flex items-center gap-1 text-sm ${
            user.credits > 0 ? 'text-zinc-700' : 'text-red-600'
          }`}
        >
          <Coins className="w-3.5 h-3.5" />
          {user.credits}
        </span>
      </div>
      <Button
        variant="ghost"
        size="icon"
        className="h-8 w-8"
        title="登出"
        data-testid="logout-button"
        onClick={() => void logout()}
      >
        <LogOut className="w-4 h-4 text-zinc-500" />
      </Button>
    </div>
  )
}
