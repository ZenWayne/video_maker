import { useRef } from 'react'
import { Upload } from 'lucide-react'

export interface VoiceCalibrationPanelProps {
  referenceVoicePath: string | null
  referenceVoiceShotId: number | null
  autoVoiceCalibrate: boolean
  onUpload: (file: File) => void
  onRemove: () => void
  onToggleAuto: (enabled: boolean) => void
  onCalibrateAll: () => void
}

export function VoiceCalibrationPanel({
  referenceVoicePath,
  referenceVoiceShotId,
  autoVoiceCalibrate,
  onUpload,
  onRemove,
  onToggleAuto,
  onCalibrateAll,
}: VoiceCalibrationPanelProps) {
  const fileRef = useRef<HTMLInputElement>(null)
  const hasBaseVoice = !!referenceVoicePath || referenceVoiceShotId != null
  const fileName = referenceVoicePath ? referenceVoicePath.split('/').pop() : null

  return (
    <div className="rounded-lg border bg-white p-4 shadow-sm space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-sm font-semibold text-zinc-800">音色校准</span>
        {hasBaseVoice && (
          <button
            className="text-xs text-zinc-400 underline hover:text-zinc-600"
            onClick={onRemove}
          >
            移除
          </button>
        )}
      </div>

      <div className="text-sm text-zinc-600">
        基准音色：{' '}
        {referenceVoicePath ? (
          <span className="font-medium text-amber-700">上传文件 {fileName}</span>
        ) : referenceVoiceShotId != null ? (
          <span className="font-medium text-amber-700">分镜 #{referenceVoiceShotId}</span>
        ) : (
          <span className="text-zinc-400">未设置（上传文件，或在某个分镜点「设为基准」）</span>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <input
          ref={fileRef}
          type="file"
          accept=".mp4,.m4a,.wav"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0]
            if (f) onUpload(f)
            e.target.value = ''
          }}
        />
        <button
          className="inline-flex items-center gap-1.5 rounded-md border border-zinc-300 bg-zinc-50 px-3 py-1.5 text-sm font-medium text-zinc-700 hover:bg-zinc-100"
          onClick={() => fileRef.current?.click()}
        >
          <Upload className="w-4 h-4" />
          上传基准音色 (mp4/m4a/wav)
        </button>

        <button
          className="rounded-md border border-zinc-300 bg-white px-3 py-1.5 text-sm font-medium text-zinc-700 hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-40"
          disabled={!hasBaseVoice}
          onClick={onCalibrateAll}
        >
          校准全部
        </button>

        <label
          className="ml-auto flex items-center gap-2 text-sm text-zinc-600"
          title={hasBaseVoice ? '' : '先设置基准音色'}
        >
          <input
            type="checkbox"
            aria-label="自动音色校准"
            className="rounded border-zinc-300"
            disabled={!hasBaseVoice}
            checked={autoVoiceCalibrate}
            onChange={(e) => onToggleAuto(e.target.checked)}
          />
          自动音色校准
          <span className="text-xs text-zinc-400">（仅对之后生成的分镜生效）</span>
        </label>
      </div>
    </div>
  )
}
