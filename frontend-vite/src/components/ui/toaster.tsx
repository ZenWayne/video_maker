// components/ui/toaster.tsx - Toast 通知组件

import { useStore } from '@/lib/state'
import { X } from 'lucide-react'
import { useEffect } from 'react'

export function Toaster() {
  const { toasts, removeToast } = useStore()

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2">
      {toasts.map((toast) => (
        <Toast key={toast.id} toast={toast} onClose={() => removeToast(toast.id)} />
      ))}
    </div>
  )
}

interface ToastProps {
  toast: {
    id: string
    type: 'success' | 'error' | 'info' | 'warning'
    message: string
  }
  onClose: () => void
}

function Toast({ toast, onClose }: ToastProps) {
  useEffect(() => {
    const timer = setTimeout(onClose, 5000)
    return () => clearTimeout(timer)
  }, [onClose])

  const bgColors = {
    success: 'bg-green-100 border-green-200 text-green-800',
    error: 'bg-red-100 border-red-200 text-red-800',
    info: 'bg-blue-100 border-blue-200 text-blue-800',
    warning: 'bg-yellow-100 border-yellow-200 text-yellow-800',
  }

  return (
    <div
      className={`flex items-center gap-3 px-4 py-3 rounded-lg border shadow-lg min-w-[300px] animate-in slide-in-from-right ${bgColors[toast.type]}`}
    >
      <span className="flex-1 text-sm">{toast.message}</span>
      <button
        onClick={onClose}
        className="p-1 hover:bg-black/10 rounded transition-colors"
      >
        <X className="w-4 h-4" />
      </button>
    </div>
  )
}
