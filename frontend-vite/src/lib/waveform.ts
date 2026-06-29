// lib/waveform.ts — 波形纯逻辑:降采样与像素↔帧映射(无 DOM 依赖,便于单测)

/** 把 PCM 单声道降采样为 `buckets` 个 [0,1] 峰值,每桶取绝对值最大。 */
export function downsamplePeaks(channel: Float32Array, buckets: number): number[] {
  const peaks = new Array<number>(buckets).fill(0)
  if (channel.length === 0 || buckets <= 0) return peaks
  const size = channel.length / buckets
  for (let i = 0; i < buckets; i++) {
    const start = Math.floor(i * size)
    const end = Math.min(channel.length, Math.floor((i + 1) * size))
    let max = 0
    for (let j = start; j < end; j++) {
      const v = Math.abs(channel[j])
      if (v > max) max = v
    }
    peaks[i] = max
  }
  return peaks
}

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
