// components/UploadZone.tsx - 参考图拖拽上传组件

'use client'

import { useState, useCallback } from 'react'
import { Upload, X } from 'lucide-react'

interface UploadZoneProps {
  kind: 'character' | 'scene'
  maxFiles: number
  value: File[]
  onChange: (files: File[]) => void
}

export function UploadZone({ kind, maxFiles, value, onChange }: UploadZoneProps) {
  const [isDragging, setIsDragging] = useState(false)

  const handleDragEnter = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDragging(true)
  }, [])

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDragging(false)
  }, [])

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
  }, [])

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault()
      e.stopPropagation()
      setIsDragging(false)

      const droppedFiles = Array.from(e.dataTransfer.files).filter((file) =>
        file.type.startsWith('image/')
      )

      if (droppedFiles.length === 0) return

      const newFiles = [...value, ...droppedFiles].slice(0, maxFiles)
      onChange(newFiles)

      if (droppedFiles.length + value.length > maxFiles) {
        alert(`最多只能上传 ${maxFiles} 张图片，已自动截取`)
      }
    },
    [value, maxFiles, onChange]
  )

  const handleFileSelect = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const selectedFiles = Array.from(e.target.files || []).filter((file) =>
        file.type.startsWith('image/')
      )

      if (selectedFiles.length === 0) return

      const newFiles = [...value, ...selectedFiles].slice(0, maxFiles)
      onChange(newFiles)

      if (selectedFiles.length + value.length > maxFiles) {
        alert(`最多只能上传 ${maxFiles} 张图片，已自动截取`)
      }
    },
    [value, maxFiles, onChange]
  )

  const removeFile = useCallback(
    (index: number) => {
      const newFiles = value.filter((_, i) => i !== index)
      onChange(newFiles)
    },
    [value, onChange]
  )

  const label = kind === 'character' ? '角色参考图' : '场景参考图'
  const required = kind === 'character'

  return (
    <div className="space-y-3">
      <label className="block text-sm font-medium text-zinc-700">
        {label}
        {required && <span className="text-red-500 ml-1">*</span>}
        <span className="text-zinc-400 ml-2">(最多 {maxFiles} 张)</span>
      </label>

      <div
        onDragEnter={handleDragEnter}
        onDragLeave={handleDragLeave}
        onDragOver={handleDragOver}
        onDrop={handleDrop}
        className={`
          border-2 border-dashed rounded-lg p-6 transition-colors cursor-pointer
          flex flex-col items-center justify-center gap-2
          ${isDragging ? 'border-blue-500 bg-blue-50' : 'border-zinc-300 hover:border-zinc-400'}
        `}
      >
        <input
          type="file"
          accept="image/*"
          multiple
          onChange={handleFileSelect}
          className="hidden"
          id={`file-input-${kind}`}
        />
        <label
          htmlFor={`file-input-${kind}`}
          className="flex flex-col items-center gap-2 cursor-pointer w-full"
        >
          <Upload className="w-8 h-8 text-zinc-400" />
          <p className="text-sm text-zinc-500 text-center">
            拖拽图片到此处，或<span className="text-blue-600">点击选择</span>
          </p>
          <p className="text-xs text-zinc-400">支持 JPG、PNG、WebP 格式</p>
        </label>
      </div>

      {value.length > 0 && (
        <div className="grid grid-cols-3 gap-3">
          {value.map((file, index) => (
            <div key={index} className="relative group">
              <img
                src={URL.createObjectURL(file)}
                alt={`预览 ${index + 1}`}
                className="w-full h-24 object-cover rounded-lg"
              />
              <button
                onClick={() => removeFile(index)}
                className="absolute -top-2 -right-2 w-6 h-6 bg-red-500 text-white rounded-full
                  flex items-center justify-center opacity-0 group-hover:opacity-100
                  transition-opacity shadow-sm hover:bg-red-600"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
