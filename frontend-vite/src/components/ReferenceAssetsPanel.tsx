import type { ReactNode } from 'react'
import { Images } from 'lucide-react'
import type { ReferenceImage } from '@/lib/types'

// Reference images are served via the /api/media static mount from their
// storage_path (the backend returns the raw storage path, not a media URL).
const refMediaUrl = (storagePath: string) =>
  `/api/media/${storagePath.replace(/^\/?storage\//, '')}`

function ThumbGroup({ label, images }: { label: string; images: ReferenceImage[] }) {
  if (images.length === 0) return null
  return (
    <div className="space-y-1.5">
      <div className="text-[11px] font-semibold text-zinc-500">{label}</div>
      <div className="flex flex-wrap gap-2.5">
        {images.map((img) => (
          <div key={img.id} className="flex w-16 flex-col gap-1">
            <img
              src={refMediaUrl(img.storage_path)}
              alt={img.filename}
              className="h-16 w-16 rounded border border-zinc-200 object-cover"
            />
            <span className="truncate text-[11px] text-zinc-400" title={img.filename}>
              {img.filename}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

export interface ReferenceAssetsPanelProps {
  images: ReferenceImage[]
  /** Voice-calibration row, rendered below a divider. Pass an embedded
   *  VoiceCalibrationPanel (shot_review only); omit on the script-review page. */
  voice?: ReactNode
}

/**
 * Two-row reference-assets panel shown at the top of the script-/shot-approval
 * pages. Row 1 = reference images (character/scene). Row 2 = voice calibration
 * (only when `voice` is provided).
 */
export function ReferenceAssetsPanel({ images, voice }: ReferenceAssetsPanelProps) {
  if (images.length === 0 && !voice) return null

  const characters = images.filter((r) => r.kind === 'character')
  const scenes = images.filter((r) => r.kind === 'scene')

  return (
    <div className="space-y-4 rounded-lg border bg-white p-4 shadow-sm">
      {/* Row 1 — Reference images */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Images className="h-4 w-4 text-zinc-700" />
            <span className="text-sm font-semibold text-zinc-900">参考图</span>
            <span className="text-sm text-zinc-400">{images.length} 张</span>
          </div>
          <span className="text-xs text-zinc-400">首帧将从「角色」参考图中选取</span>
        </div>
        {images.length > 0 ? (
          <div className="space-y-3">
            <ThumbGroup label="角色 · CHARACTER" images={characters} />
            <ThumbGroup label="场景 · SCENE" images={scenes} />
          </div>
        ) : (
          <div className="text-xs text-zinc-400">
            暂无参考图 — 在新建项目页上传角色 / 场景参考图
          </div>
        )}
      </div>

      {/* Row 2 — Voice calibration (shot_review only) */}
      {voice && <div className="border-t border-zinc-100 pt-3">{voice}</div>}
    </div>
  )
}
