// components/UserBadge.tsx - 用户名显示/修改组件

'use client'

import { useState } from 'react'
import { User, Check, X } from 'lucide-react'
import { useStore } from '@/lib/state'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

export function UserBadge() {
  const { userName, setUserName } = useStore()
  const [isEditing, setIsEditing] = useState(false)
  const [tempName, setTempName] = useState(userName)

  const handleSave = () => {
    if (tempName.trim()) {
      setUserName(tempName.trim())
      setIsEditing(false)
    }
  }

  const handleCancel = () => {
    setTempName(userName)
    setIsEditing(false)
  }

  if (isEditing) {
    return (
      <div className="flex items-center gap-2"
      >
        <Input
          value={tempName}
          onChange={(e) => setTempName(e.target.value)}
          placeholder="输入用户名"
          className="h-8 w-40"
          autoFocus
          onKeyDown={(e) => {
            if (e.key === 'Enter') handleSave()
            if (e.key === 'Escape') handleCancel()
          }}
        />
        <Button variant="ghost" size="icon" className="h-8 w-8" onClick={handleSave}>
          <Check className="w-4 h-4 text-green-600" />
        </Button>
        <Button variant="ghost" size="icon" className="h-8 w-8" onClick={handleCancel}>
          <X className="w-4 h-4 text-red-600" />
        </Button>
      </div>
    )
  }

  return (
    <button
      onClick={() => setIsEditing(true)}
      className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-zinc-100 hover:bg-zinc-200 transition-colors"
    >
      <User className="w-4 h-4 text-zinc-500" />
      <span className="text-sm text-zinc-700">
        {userName || '点击设置用户名'}
      </span>
    </button>
  )
}
