interface Props {
  spriteUrl: string | null
  trimFrac: number // 0..1，保留比例
  height?: number
}

/** 视频胶片轨（纯展示）：sprite 背景 + 右侧裁掉磨砂遮罩。指针逻辑由容器负责。 */
export function VideoFilmstripTrack({ spriteUrl, trimFrac, height = 60 }: Props) {
  const trimmedPct = `${Math.max(0, Math.min(1, 1 - trimFrac)) * 100}%`
  return (
    <div data-testid="video-track" className="relative rounded-md overflow-hidden bg-zinc-200" style={{ height }}>
      {spriteUrl ? (
        <div className="absolute inset-0" style={{ backgroundImage: `url(${spriteUrl})`, backgroundSize: '100% 100%' }} />
      ) : (
        <div data-testid="video-track-fallback" className="absolute inset-0 bg-gradient-to-b from-indigo-200 to-indigo-400" />
      )}
      <div data-testid="video-trim-overlay" className="absolute inset-y-0 right-0" style={{ width: trimmedPct, background: '#F4F4F5CC' }} />
    </div>
  )
}
