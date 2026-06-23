import { useRef } from 'react'

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
    <div className="rounded-lg border border-neutral-700 bg-neutral-900/50 p-3 space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-neutral-200">音色校准</span>
      </div>

      <div className="text-sm text-neutral-300">
        基准音色:{' '}
        {referenceVoicePath ? (
          <span className="text-amber-400">上传文件: {fileName}</span>
        ) : referenceVoiceShotId != null ? (
          <span className="text-amber-400">分镜 {referenceVoiceShotId}</span>
        ) : (
          <span className="text-neutral-500">未设置（上传文件，或在某个分镜点「设为基准」）</span>
        )}
        {hasBaseVoice && (
          <button className="ml-2 text-xs text-neutral-400 underline" onClick={onRemove}>
            移除
          </button>
        )}
      </div>

      <div>
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
          className="rounded bg-neutral-700 px-3 py-1.5 text-sm text-neutral-100 hover:bg-neutral-600"
          onClick={() => fileRef.current?.click()}
        >
          ⬆ 上传基准音色 (mp4/m4a/wav)
        </button>
      </div>

      <label className="flex items-center gap-2 text-sm text-neutral-300" title={hasBaseVoice ? '' : '先设置基准音色'}>
        <input
          type="checkbox"
          aria-label="自动音色校准"
          disabled={!hasBaseVoice}
          checked={autoVoiceCalibrate}
          onChange={(e) => onToggleAuto(e.target.checked)}
        />
        自动音色校准
        <span className="text-xs text-neutral-500">（仅对之后生成的分镜生效）</span>
      </label>

      <button
        className="rounded border border-neutral-600 px-3 py-1.5 text-sm text-neutral-200 disabled:opacity-40"
        disabled={!hasBaseVoice}
        onClick={onCalibrateAll}
      >
        校准全部
      </button>
    </div>
  )
}
