// lib/waveform.ts — 波形纯逻辑:像素↔帧映射(无 DOM 依赖,便于单测)
// 注:峰值降采样已移至后端 ffmpeg 提取(GET /api/projects/:id/shots/:shot_id/waveform)。

/** 鼠标相对轨道左缘 x 像素 → 帧号(线性,钳制 [0,totalFrames],四舍五入)。 */
export function frameFromOffsetX(
  offsetX: number,
  trackWidth: number,
  totalFrames: number,
): number {
  if (trackWidth <= 0) return 0
  const ratio = Math.min(1, Math.max(0, offsetX / trackWidth))
  return Math.round(ratio * totalFrames)
}

/** 帧号 → x 像素(线性,无内边距,与滑块 (frame/total)*width 对齐)。 */
export function pixelForFrame(
  frame: number,
  trackWidth: number,
  totalFrames: number,
): number {
  if (totalFrames <= 0) return 0
  return (frame / totalFrames) * trackWidth
}
