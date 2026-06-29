# 裁剪弹窗声纹波形轨 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在裁剪弹窗的视频区与帧滑块之间,加一条与时间轴对齐的声纹波形轨,让用户直观对比尾部静音并精确设裁剪点。

**Architecture:** 波形由前端 Web Audio `decodeAudioData` 从 `shot.video_path` 解码渲染(纯客户端);后端 `video-info` 端点复用既有 `detect_speech_end` 暴露「说话结束帧」,用作只读的黄线/静音高亮;红色裁剪手柄在波形上点击/拖拽,复用 `TrimDialog` 现有 `handleSliderChange` 与滑块/帧标签联动。

**Tech Stack:** 后端 FastAPI + `python-ffmpeg`(lavfi 合成测试视频)+ pytest;前端 React + TypeScript + Vitest + Testing Library;Canvas 2D + Web Audio API。

**设计依据:** spec `docs/superpowers/specs/2026-06-28-silence-waveform-trim-design.md`;Pencil 稿 `design/shots.pen` 节点 `N3hb4`。

## Global Constraints

- Python 包用 `pyproject.toml` 管理,脚本/测试用 `uv run`(如 `uv run --project backend pytest ...`),禁止直接 `python`/`pip`。
- 禁止硬编码绝对路径:Python 用 `Path(__file__)`,TS 用相对路径。
- 测试只 mock 花钱的 LLM/模型调用;ffmpeg、DB 等用真实服务。
- 后端 `google.genai` 必须 `vertexai=True`(本计划不涉及,仅作约束记录)。
- 任何改 shot 素材文件的代码须做素材审计——本计划**只读** `shot.video_path`,不写/改/删素材(spec §8 已审计)。
- 颜色/字体沿用设计系统:`blue-500`=人声/进度,`red-500`=裁剪点,`amber`=说话结束,`zinc-*`=中性,Inter,圆角 8。

---

### Task 1: 后端 `speech_end_frame` helper + `video-info` 端点暴露说话结束帧

**Files:**
- Modify: `backend/app/agents/video_trimmer.py`(在 `detect_speech_end` 之后新增纯函数 `speech_end_frame`)
- Modify: `backend/app/api/pipeline.py:1102-1124`(`get_shot_video_info` 端点追加字段)
- Test: `backend/tests/unit/test_speech_end_frame.py`(新建)

**Interfaces:**
- Consumes: 既有 `detect_speech_end(video_path) -> float | None`、`get_video_info(video_path) -> dict`(键 `fps/total_frames/duration`)。
- Produces:
  - `speech_end_info(video_path: str, fps: float) -> tuple[float | None, int | None]` —— 返回 (尾部静音起点秒, 对应帧号);无尾部静音返回 `(None, None)`。**生产端点与单测都调用它**(避免死代码、单次 ffmpeg 调用)。
  - `video-info` 端点响应新增键:`speech_end_sec: float | None`、`speech_end_frame: int | None`。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/unit/test_speech_end_frame.py`:

```python
"""Unit tests for speech_end_frame helper.

合成视频用 ffmpeg lavfi(sine 人声 + apad 尾部静音);无 ffmpeg 时跳过。
"""

import shutil
import pytest
from pathlib import Path

from ffmpeg import FFmpeg

from app.agents.video_trimmer import speech_end_info, get_video_info

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg binary not found in PATH",
)


def _make_video_trailing_silence(path: Path, speech: float = 1.5, total: float = 2.5) -> None:
    """前 `speech` 秒 440Hz 正弦,之后 apad 补静音直到 `total` 秒。"""
    (
        FFmpeg()
        .option("y")
        .input(f"color=blue:size=64x64:rate=24:duration={total}", f="lavfi")
        .input(f"sine=frequency=440:duration={speech}", f="lavfi")
        .output(
            str(path),
            t=total,
            af="apad",
            pix_fmt="yuv420p",
            vcodec="libx264",
            acodec="aac",
        )
    ).execute()


def _make_video_full_speech(path: Path, total: float = 2.0) -> None:
    """全程正弦,无尾部静音。"""
    (
        FFmpeg()
        .option("y")
        .input(f"color=blue:size=64x64:rate=24:duration={total}", f="lavfi")
        .input(f"sine=frequency=440:duration={total}", f="lavfi")
        .output(
            str(path),
            pix_fmt="yuv420p",
            vcodec="libx264",
            acodec="aac",
            shortest=None,
        )
    ).execute()


def test_returns_frame_near_speech_end(tmp_path):
    video = tmp_path / "trailing.mp4"
    _make_video_trailing_silence(video, speech=1.5, total=2.5)
    fps = get_video_info(str(video))["fps"]

    sec, frame = speech_end_info(str(video), fps)

    assert sec is not None
    assert frame is not None
    # 说话约在 1.5s 结束,24fps → ~36 帧,给静音检测留 ±0.4s 容差
    assert 26 <= frame <= 46


def test_returns_none_when_no_trailing_silence(tmp_path):
    video = tmp_path / "full.mp4"
    _make_video_full_speech(video, total=2.0)
    fps = get_video_info(str(video))["fps"]

    assert speech_end_info(str(video), fps) == (None, None)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest backend/tests/unit/test_speech_end_frame.py -v`
Expected: FAIL —— `ImportError: cannot import name 'speech_end_info'`(若本机无 ffmpeg 则全部 SKIP,需在有 ffmpeg 的环境跑)。

- [ ] **Step 3: 实现 `speech_end_info`**

在 `backend/app/agents/video_trimmer.py` 的 `detect_speech_end` 函数之后追加:

```python
def speech_end_info(video_path: str, fps: float) -> tuple[float | None, int | None]:
    """(尾部静音起点秒, 对应帧号);无尾部静音返回 (None, None)。

    复用 detect_speech_end(-30dB / 0.3s),与「智能校准」同一套检测,
    便于前端波形上对比手动裁剪点与自动裁剪点。
    """
    sec = detect_speech_end(video_path)
    if sec is None:
        return None, None
    return sec, int(sec * fps)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --project backend pytest backend/tests/unit/test_speech_end_frame.py -v`
Expected: PASS(2 passed)。

- [ ] **Step 5: 接入 `video-info` 端点**

修改 `backend/app/api/pipeline.py` 的 `get_shot_video_info`(约 1109-1124 行)。导入处与函数体改为:

```python
    from app.agents.video_trimmer import get_video_info, speech_end_info

    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot or not shot.video_path:
        raise HTTPException(status_code=404, detail="Shot or video not found")

    info = get_video_info(shot.video_path)
    backup = Path(shot.video_path).with_name("output_original.mp4")
    info["has_backup"] = backup.exists()
    try:
        sec, frame = speech_end_info(shot.video_path, info["fps"])
    except Exception:  # 静音检测失败不应阻塞裁剪元数据返回
        sec, frame = None, None
    info["speech_end_sec"] = sec
    info["speech_end_frame"] = frame
    return info
```

> 说明:`speech_end_info` 同时被生产端点与单测调用(无死代码),内部只跑一次 `detect_speech_end`;端点已有 `info["fps"]` 直接传入。整体包在 try/except 内,静音检测异常时降级为 `(None, None)`,不影响 fps/frames/duration。

- [ ] **Step 6: 提交**

```bash
git add backend/app/agents/video_trimmer.py backend/app/api/pipeline.py backend/tests/unit/test_speech_end_frame.py
git commit -m "feat(trim): video-info 暴露 speech_end_frame/sec(复用 detect_speech_end)"
```

---

### Task 2: 前端类型与 api 扩展

**Files:**
- Modify: `frontend-vite/src/lib/types.ts:3-7`(`VideoInfo` 接口)
- Modify: `frontend-vite/src/lib/api.ts:300-302`(`getVideoInfo` 返回类型)

**Interfaces:**
- Produces: `VideoInfo` 与 `getVideoInfo` 返回类型新增 `speech_end_frame: number | null`、`speech_end_sec: number | null`。后续 Task 4/5 依赖。

> 本任务为纯类型扩展,无独立运行时行为;验证靠 `tsc` 类型检查,不单列测试步骤。

- [ ] **Step 1: 扩展 `VideoInfo`**

`frontend-vite/src/lib/types.ts` 第 3-7 行改为:

```typescript
export interface VideoInfo {
  fps: number
  total_frames: number
  duration: number
  speech_end_frame: number | null
  speech_end_sec: number | null
}
```

- [ ] **Step 2: 扩展 `getVideoInfo` 返回类型**

`frontend-vite/src/lib/api.ts` 第 300-302 行(`getVideoInfo`)的返回类型补字段:

```typescript
  getVideoInfo: (projectId: string, shotId: number): Promise<{
    fps: number
    total_frames: number
    duration: number
    has_backup: boolean
    speech_end_frame: number | null
    speech_end_sec: number | null
  }> => {
    return request('GET', `/api/projects/${projectId}/shots/${shotId}/video-info`)
  },
```

- [ ] **Step 3: 类型检查通过**

Run: `cd frontend-vite && npx tsc --noEmit`
Expected: 无新增错误(既有 `TrimDialog` 解构 `getVideoInfo` 结果不受影响,新增字段为可选读取)。

- [ ] **Step 4: 提交**

```bash
git add frontend-vite/src/lib/types.ts frontend-vite/src/lib/api.ts
git commit -m "feat(trim): 前端类型补 speech_end_frame/speech_end_sec"
```

---

### Task 3: 波形纯逻辑 helper(降采样 + 像素↔帧映射)

**Files:**
- Create: `frontend-vite/src/lib/waveform.ts`
- Test: `frontend-vite/src/lib/__tests__/waveform.test.ts`(新建)

**Interfaces:**
- Produces:
  - `downsamplePeaks(channel: Float32Array, buckets: number): number[]` —— 把 PCM 单声道降采样为 `buckets` 个 `[0,1]` 峰值(每桶取绝对值最大)。
  - `frameFromOffsetX(offsetX: number, trackWidth: number, totalFrames: number): number` —— 鼠标相对轨道左缘的 x 像素 → 帧号(钳制到 `[0, totalFrames]`,四舍五入)。
  - `pixelForFrame(frame: number, trackWidth: number, totalFrames: number): number` —— 帧号 → x 像素(线性,无内边距,确保与滑块对齐)。
  - 这些被 Task 4 的 `WaveformTrack` 消费。

- [ ] **Step 1: 写失败测试**

新建 `frontend-vite/src/lib/__tests__/waveform.test.ts`:

```typescript
import { describe, it, expect } from 'vitest'
import { downsamplePeaks, frameFromOffsetX, pixelForFrame } from '../waveform'

describe('downsamplePeaks', () => {
  it('降到指定桶数', () => {
    const ch = new Float32Array(1000).fill(0.5)
    expect(downsamplePeaks(ch, 10)).toHaveLength(10)
  })

  it('每桶取绝对值最大', () => {
    const ch = new Float32Array([0.1, -0.9, 0.2, 0.3])
    // 2 桶:[0.1,-0.9] -> 0.9, [0.2,0.3] -> 0.3
    // Float32 精度下值非精确(-0.9 存为 0.89999997),用 toBeCloseTo 逐元素比较
    const peaks = downsamplePeaks(ch, 2)
    expect(peaks).toHaveLength(2)
    expect(peaks[0]).toBeCloseTo(0.9)
    expect(peaks[1]).toBeCloseTo(0.3)
  })

  it('空输入返回全 0 数组', () => {
    expect(downsamplePeaks(new Float32Array(0), 3)).toEqual([0, 0, 0])
  })
})

describe('frameFromOffsetX', () => {
  it('左缘 → 0 帧', () => {
    expect(frameFromOffsetX(0, 500, 240)).toBe(0)
  })

  it('右缘 → totalFrames', () => {
    expect(frameFromOffsetX(500, 500, 240)).toBe(240)
  })

  it('中点 → 一半帧(四舍五入)', () => {
    expect(frameFromOffsetX(250, 500, 240)).toBe(120)
  })

  it('越界钳制', () => {
    expect(frameFromOffsetX(-50, 500, 240)).toBe(0)
    expect(frameFromOffsetX(999, 500, 240)).toBe(240)
  })
})

describe('pixelForFrame', () => {
  it('与 frameFromOffsetX 互逆(端点)', () => {
    expect(pixelForFrame(0, 500, 240)).toBe(0)
    expect(pixelForFrame(240, 500, 240)).toBe(500)
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend-vite && npx vitest run src/lib/__tests__/waveform.test.ts`
Expected: FAIL —— 模块 `../waveform` 不存在。

- [ ] **Step 3: 实现 `waveform.ts`**

新建 `frontend-vite/src/lib/waveform.ts`:

```typescript
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend-vite && npx vitest run src/lib/__tests__/waveform.test.ts`
Expected: PASS（全部用例通过）。

- [ ] **Step 5: 提交**

```bash
git add frontend-vite/src/lib/waveform.ts frontend-vite/src/lib/__tests__/waveform.test.ts
git commit -m "feat(trim): 波形纯逻辑 helper(降采样 + 像素↔帧映射)"
```

---

### Task 4: `WaveformTrack` 组件(解码 + 渲染 + 交互 + 降级)

**Files:**
- Create: `frontend-vite/src/components/WaveformTrack.tsx`
- Test: `frontend-vite/src/components/__tests__/WaveformTrack.test.tsx`(新建)

**Interfaces:**
- Consumes: `downsamplePeaks`、`frameFromOffsetX`、`pixelForFrame`(Task 3)。
- Produces: 默认导出 React 组件
  ```typescript
  interface WaveformTrackProps {
    videoSrc: string
    totalFrames: number
    endFrame: number
    speechEndFrame: number | null
    onScrub: (frame: number) => void
  }
  ```
  被 Task 5 的 `TrimDialog` 渲染。

**实现要点:**
- `useEffect` 在 `videoSrc` 变化时 `fetch(videoSrc).arrayBuffer()` → `new AudioContext().decodeAudioData()` → 取 `getChannelData(0)` → `downsamplePeaks` → 存 ref → 触发重画;完成后 `ctx.close()`。
- 状态机:`loading` / `ready` / `unavailable`(无音轨或解码/抓取失败)。`unavailable` 时返回 `null`(隐藏波形,TrimDialog 退回纯滑块)。
- canvas 上:画峰值柱(峰值低于全局峰值 5% → `zinc-300`,否则 `blue-500`);若 `speechEndFrame != null` 画 amber 静音高亮带(从 `speechEndFrame` 到末尾)+ amber 竖线;画 `red-500` 裁剪竖线(位置 `pixelForFrame(endFrame,...)`);裁剪线右侧画 red 12% 待裁区。
- `onPointerDown`/`onPointerMove`(按下时)→ `frameFromOffsetX(e.nativeEvent.offsetX, width, totalFrames)` → `onScrub`。

- [ ] **Step 1: 写失败测试**

新建 `frontend-vite/src/components/__tests__/WaveformTrack.test.tsx`:

```typescript
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import WaveformTrack from '../WaveformTrack'

// --- Web Audio / fetch / canvas 在 jsdom 中不存在,需 mock ---
const decodeAudioData = vi.fn()
const close = vi.fn().mockResolvedValue(undefined)

beforeEach(() => {
  decodeAudioData.mockReset()
  close.mockReset().mockResolvedValue(undefined)

  vi.stubGlobal(
    'AudioContext',
    vi.fn().mockImplementation(() => ({ decodeAudioData, close })),
  )
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)) }),
  )
  // canvas 2d context stub
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
    clearRect: vi.fn(),
    fillRect: vi.fn(),
    fillStyle: '',
  } as unknown as CanvasRenderingContext2D)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function fakeBuffer(): AudioBuffer {
  return {
    numberOfChannels: 1,
    length: 100,
    sampleRate: 24000,
    duration: 100 / 24000,
    getChannelData: () => new Float32Array(100).fill(0.5),
  } as unknown as AudioBuffer
}

describe('WaveformTrack', () => {
  it('解码成功后渲染 canvas', async () => {
    decodeAudioData.mockResolvedValue(fakeBuffer())
    render(
      <WaveformTrack
        videoSrc="/fake/video.mp4"
        totalFrames={240}
        endFrame={240}
        speechEndFrame={180}
        onScrub={() => {}}
      />,
    )
    await waitFor(() =>
      expect(document.querySelector('canvas')).toBeInTheDocument(),
    )
  })

  it('点击波形上报对应帧', async () => {
    decodeAudioData.mockResolvedValue(fakeBuffer())
    const onScrub = vi.fn()
    render(
      <WaveformTrack
        videoSrc="/fake/video.mp4"
        totalFrames={240}
        endFrame={240}
        speechEndFrame={180}
        onScrub={onScrub}
      />,
    )
    const canvas = await waitFor(() => {
      const c = document.querySelector('canvas')
      if (!c) throw new Error('no canvas')
      return c as HTMLCanvasElement
    })
    // jsdom 下 offsetWidth=0,组件应有兜底宽度;mock 一个尺寸
    Object.defineProperty(canvas, 'offsetWidth', { value: 500, configurable: true })
    fireEvent.pointerDown(canvas, { clientX: 250 })
    expect(onScrub).toHaveBeenCalled()
  })

  it('解码失败时降级隐藏(返回 null,无 canvas)', async () => {
    decodeAudioData.mockRejectedValue(new Error('no audio track'))
    const { container } = render(
      <WaveformTrack
        videoSrc="/fake/video.mp4"
        totalFrames={240}
        endFrame={240}
        speechEndFrame={null}
        onScrub={() => {}}
      />,
    )
    await waitFor(() =>
      expect(container.querySelector('canvas')).not.toBeInTheDocument(),
    )
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/WaveformTrack.test.tsx`
Expected: FAIL —— 模块 `../WaveformTrack` 不存在。

- [ ] **Step 3: 实现 `WaveformTrack.tsx`**

新建 `frontend-vite/src/components/WaveformTrack.tsx`:

```tsx
import { useEffect, useRef, useState, useCallback } from 'react'
import { downsamplePeaks, frameFromOffsetX, pixelForFrame } from '@/lib/waveform'

interface WaveformTrackProps {
  videoSrc: string
  totalFrames: number
  endFrame: number
  speechEndFrame: number | null
  onScrub: (frame: number) => void
}

const TRACK_HEIGHT = 84
const BAR_WIDTH = 3
const BAR_GAP = 2
const SILENCE_RATIO = 0.05 // 低于全局峰值 5% 的柱视为静音(灰)
const FALLBACK_WIDTH = 500

type Status = 'loading' | 'ready' | 'unavailable'

export default function WaveformTrack({
  videoSrc,
  totalFrames,
  endFrame,
  speechEndFrame,
  onScrub,
}: WaveformTrackProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const peaksRef = useRef<number[]>([])
  const draggingRef = useRef(false)
  const [status, setStatus] = useState<Status>('loading')

  // ---- 解码音频(videoSrc 变化时)----
  useEffect(() => {
    let cancelled = false
    setStatus('loading')
    const ctx = new AudioContext()
    fetch(videoSrc)
      .then((r) => r.arrayBuffer())
      .then((buf) => ctx.decodeAudioData(buf))
      .then((audio) => {
        if (cancelled) return
        const width = canvasRef.current?.offsetWidth || FALLBACK_WIDTH
        const buckets = Math.max(1, Math.floor(width / (BAR_WIDTH + BAR_GAP)))
        peaksRef.current = downsamplePeaks(audio.getChannelData(0), buckets)
        setStatus('ready')
      })
      .catch(() => {
        if (!cancelled) setStatus('unavailable')
      })
      .finally(() => {
        ctx.close().catch(() => {})
      })
    return () => {
      cancelled = true
    }
  }, [videoSrc])

  // ---- 画 canvas(峰值/状态变化时)----
  const draw = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas || status !== 'ready') return
    const width = canvas.offsetWidth || FALLBACK_WIDTH
    canvas.width = width
    canvas.height = TRACK_HEIGHT
    const g = canvas.getContext('2d')
    if (!g) return
    g.clearRect(0, 0, width, TRACK_HEIGHT)

    const peaks = peaksRef.current
    const globalMax = peaks.reduce((m, p) => (p > m ? p : m), 0) || 1
    const mid = TRACK_HEIGHT / 2

    // 静音高亮带 + 说话结束竖线
    if (speechEndFrame != null) {
      const sx = pixelForFrame(speechEndFrame, width, totalFrames)
      g.fillStyle = 'rgba(252, 211, 77, 0.18)' // amber 18%
      g.fillRect(sx, 0, width - sx, TRACK_HEIGHT)
      g.fillStyle = '#B45309' // amber-700
      g.fillRect(sx - 1, 0, 2, TRACK_HEIGHT)
    }

    // 峰值柱
    const step = BAR_WIDTH + BAR_GAP
    peaks.forEach((p, i) => {
      const norm = p / globalMax
      const h = Math.max(2, norm * (TRACK_HEIGHT - 16))
      g.fillStyle = norm < SILENCE_RATIO ? '#D4D4D8' : '#3B82F6' // zinc-300 / blue-500
      g.fillRect(i * step, mid - h / 2, BAR_WIDTH, h)
    })

    // 待裁区 + 裁剪竖线
    const cx = pixelForFrame(endFrame, width, totalFrames)
    g.fillStyle = 'rgba(239, 68, 68, 0.12)' // red 12%
    g.fillRect(cx, 0, width - cx, TRACK_HEIGHT)
    g.fillStyle = '#EF4444' // red-500
    g.fillRect(cx - 1, 0, 3, TRACK_HEIGHT)
  }, [status, endFrame, speechEndFrame, totalFrames])

  useEffect(() => {
    draw()
  }, [draw])

  // ---- 交互:点击/拖拽设裁剪点 ----
  const scrubTo = useCallback(
    (e: React.PointerEvent<HTMLCanvasElement>) => {
      const width = canvasRef.current?.offsetWidth || FALLBACK_WIDTH
      const rect = canvasRef.current?.getBoundingClientRect()
      const offsetX = rect ? e.clientX - rect.left : 0
      onScrub(frameFromOffsetX(offsetX, width, totalFrames))
    },
    [onScrub, totalFrames],
  )

  if (status === 'unavailable') return null

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold text-zinc-600">声纹波形</span>
        <span className="text-[11px] text-zinc-400">
          蓝=人声 · 黄线=说话结束 · 红线=裁剪点
        </span>
      </div>
      <canvas
        ref={canvasRef}
        style={{ width: '100%', height: TRACK_HEIGHT }}
        className="rounded-lg bg-zinc-50 border border-zinc-200 cursor-ew-resize touch-none"
        onPointerDown={(e) => {
          draggingRef.current = true
          e.currentTarget.setPointerCapture(e.pointerId)
          scrubTo(e)
        }}
        onPointerMove={(e) => {
          if (draggingRef.current) scrubTo(e)
        }}
        onPointerUp={(e) => {
          draggingRef.current = false
          e.currentTarget.releasePointerCapture(e.pointerId)
        }}
      />
      {status === 'loading' && (
        <span className="text-[11px] text-zinc-400">声纹解码中…</span>
      )}
    </div>
  )
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/WaveformTrack.test.tsx`
Expected: PASS（3 passed）。

> 若「点击上报帧」用例因 jsdom 无 `setPointerCapture` 报错,在测试 `beforeEach` 补 `HTMLElement.prototype.setPointerCapture = vi.fn()` 与 `releasePointerCapture = vi.fn()`。

- [ ] **Step 5: 提交**

```bash
git add frontend-vite/src/components/WaveformTrack.tsx frontend-vite/src/components/__tests__/WaveformTrack.test.tsx
git commit -m "feat(trim): WaveformTrack 组件(Web Audio 解码 + canvas 渲染 + 拖拽裁剪 + 降级)"
```

---

### Task 5: 把波形轨集成进 `TrimDialog`

**Files:**
- Modify: `frontend-vite/src/components/TrimDialog.tsx`(新增 `speechEndFrame` state、渲染 `WaveformTrack`)
- Modify: `frontend-vite/src/components/__tests__/TrimDialog.test.tsx`(补 mock 字段 + 集成断言)

**Interfaces:**
- Consumes: `WaveformTrack`(Task 4)、`getVideoInfo` 新字段(Task 2)。
- Produces: 无新对外接口;裁剪流程行为不变。

- [ ] **Step 1: 写失败测试**

修改 `frontend-vite/src/components/__tests__/TrimDialog.test.tsx`:

(a) 顶部 `vi.mock('@/lib/api', ...)` 里 `getVideoInfo` 的 resolved 值补两个字段:

```typescript
    getVideoInfo: vi.fn().mockResolvedValue({
      fps: 24,
      total_frames: 240,
      duration: 10.0,
      has_backup: false,
      speech_end_frame: 180,
      speech_end_sec: 7.5,
    }),
```

(b) 因为 `WaveformTrack` 会用 `AudioContext`/`fetch`/canvas,在该测试文件 `beforeEach` 顶部补全局 stub(避免渲染报错):

```typescript
  vi.stubGlobal('AudioContext', vi.fn().mockImplementation(() => ({
    decodeAudioData: vi.fn().mockResolvedValue({
      numberOfChannels: 1, length: 10, sampleRate: 24000, duration: 0,
      getChannelData: () => new Float32Array(10).fill(0.3),
    }),
    close: vi.fn().mockResolvedValue(undefined),
  })))
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)),
  }))
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
    clearRect: vi.fn(), fillRect: vi.fn(), fillStyle: '',
  } as unknown as CanvasRenderingContext2D)
```

(c) 新增一个用例(放在已有 `describe` 内):

```typescript
  it('加载后渲染声纹波形轨', async () => {
    render(
      <TrimDialog
        shot={mockShot}
        projectId="proj-1"
        open={true}
        onOpenChange={() => {}}
        onTrimmed={() => {}}
      />,
    )
    expect(await screen.findByText('声纹波形')).toBeInTheDocument()
  })
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/TrimDialog.test.tsx -t '声纹波形'`
Expected: FAIL —— 找不到文本「声纹波形」(尚未渲染 `WaveformTrack`)。

- [ ] **Step 3: 集成到 `TrimDialog`**

(a) 顶部 import 区加:

```tsx
import WaveformTrack from '@/components/WaveformTrack'
```

(b) 在 state 区(约 `const [endFrame, setEndFrame] = useState(0)` 附近)加:

```tsx
  const [speechEndFrame, setSpeechEndFrame] = useState<number | null>(null)
```

(c) 在 `getVideoInfo().then((info) => { ... })` 回调内(`setHasBackup` 之后)加:

```tsx
      setSpeechEndFrame(info.speech_end_frame)
```

(d) 在视频预览 `</div>`(`{/* Video preview */}` 那个块)之后、`{/* Slider with trim indicator */}` 之前,插入:

```tsx
            {/* Waveform track — 与下方滑块共享时间轴 */}
            <div className="shrink-0">
              <WaveformTrack
                videoSrc={shot.video_path || ''}
                totalFrames={totalFrames}
                endFrame={endFrame}
                speechEndFrame={speechEndFrame}
                onScrub={handleSliderChange}
              />
            </div>
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/TrimDialog.test.tsx`
Expected: PASS（含新用例与原有用例;原有裁剪/预览/步进用例不回归)。

- [ ] **Step 5: 类型检查 + 全量前端测试**

Run: `cd frontend-vite && npx tsc --noEmit && npx vitest run`
Expected: 类型无误,测试全绿。

- [ ] **Step 6: 提交**

```bash
git add frontend-vite/src/components/TrimDialog.tsx frontend-vite/src/components/__tests__/TrimDialog.test.tsx
git commit -m "feat(trim): TrimDialog 集成声纹波形轨,onScrub 复用 handleSliderChange"
```

---

### Task 6: 端到端手测验证(真实栈)

**Files:** 无代码改动,仅验证。

- [ ] **Step 1: 起后端栈**

Run: `podman compose -f deploy/docker-compose.dev.yml up -d`
确认 backend/worker/redis 起来。

- [ ] **Step 2: 重启使后端改动生效**

Run: `podman restart $(podman ps --format '{{.Names}}' | grep -E 'backend|worker')`

- [ ] **Step 3: 起前端**

Run: `make dev`(或 `cd frontend-vite && npm run dev`),浏览器打开某个有 `video_path` 且**有音轨**的 shot。

- [ ] **Step 4: 验证清单**

- [ ] 打开裁剪弹窗,视频区下方出现声纹波形轨,蓝柱反映人声、尾部为矮灰柱。
- [ ] 有尾部静音的 shot:出现黄色「说话结束」竖线 + 静音高亮带;拖红线对齐黄线,帧/时间标签与下方滑块同步变化。
- [ ] 点击波形任意位置,红线跳到该处,`endFrame`/滑块/视频帧同步。
- [ ] 「确认裁剪」「智能校准」「还原」行为与改动前一致。
- [ ] 换一个**无音轨**的 shot(若有):波形轨自动隐藏,纯滑块裁剪仍可用、不报错(看 console 无异常)。

- [ ] **Step 5: 完成(无需提交,纯验证)**

若发现问题,回到对应 Task 修复并补测。

---

## Self-Review

**1. Spec coverage（逐节核对 spec → 任务）:**
- spec §3 振幅柱(前端 Web Audio) → Task 3(降采样)+ Task 4(解码/渲染) ✓
- spec §3 静音高亮 + 说话结束线(后端 detect_speech_end 经 video-info) → Task 1 + Task 4(画黄线/高亮) ✓
- spec §3 红色裁剪手柄(可拖/点击,联动) → Task 4(交互)+ Task 5(onScrub=handleSliderChange) ✓
- spec §4.2 video-info 暴露 speech_end_frame/sec → Task 1 ✓
- spec §4.3 api/types 扩展 + TrimDialog 集成 → Task 2 + Task 5 ✓
- spec §4.4 波形与滑块共享时间轴对齐 → Task 3 `pixelForFrame` 线性无内边距 + Task 5 注释 ✓
- spec §6 状态/边界(loading/无音轨降级/speech_end=null) → Task 4 status 机 + Task 1 None 分支 + Task 6 手测 ✓
- spec §7 测试(后端 fixture / 前端各项) → Task 1/3/4/5 测试步骤 ✓
- spec §8 素材审计(只读) → Global Constraints + 仅读 video_path,无写 ✓

**2. Placeholder scan:** 无 TBD/TODO;每个代码步骤含完整可运行代码与预期输出。✓

**3. Type consistency:** helper `speech_end_info` 在 Task 1 定义、端点与单测共用;响应键 `speech_end_frame`/`speech_end_sec` 在 Task 1(后端键)、Task 2(TS 类型)、Task 5(读取 `info.speech_end_frame`)一致;`downsamplePeaks`/`frameFromOffsetX`/`pixelForFrame` 在 Task 3 定义、Task 4 消费,签名一致;`WaveformTrackProps`(videoSrc/totalFrames/endFrame/speechEndFrame/onScrub)在 Task 4 定义、Task 5 传参一致。✓

---

## Addendum (2026-06-29):数据源转向后端 ffmpeg 提峰值

**背景**:Task 6 真实栈 e2e 发现前端 Web Audio `decodeAudioData` 对生产 shot MP4(带视频轨的 muxed 容器)恒返回 `EncodingError`(已在 3 个文件确认),波形静默卸载、功能落空。单元测试因 mock 了 `decodeAudioData` 而未暴露。经用户决策:**改为后端 ffmpeg 抽音轨算峰值,前端只渲染**。`detect_speech_end`/黄线/`video-info`(Task 1)真实验证正常,保持不变。

### Task 7:后端波形峰值提取 + `/waveform` 端点

**Files:**
- Modify: `backend/app/agents/video_trimmer.py`(新增 `extract_waveform_peaks`)
- Modify: `backend/app/api/pipeline.py`(新增 `GET .../shots/{shot_id}/waveform` 端点)
- Test: `backend/tests/unit/test_waveform_peaks.py`(新建)

**Interfaces:**
- Produces:
  - `extract_waveform_peaks(video_path: str, buckets: int = 200) -> list[float]` —— 用 ffmpeg 抽单声道 PCM(`-ac 1 -ar 8000 -f s16le -`),分 `buckets` 桶取每桶 `max(abs)/32768` 归一化到 `[0,1]`;无音轨/失败返回 `[]`。
  - 端点 `GET /api/projects/{project_id}/shots/{shot_id}/waveform` → `{"peaks": list[float]}`。

**TDD 要点(真实 ffmpeg,合成视频复用 Task 1 的 lavfi/concat helper 思路):**
- 对**有人声**的合成视频(sine + 静音段),`extract_waveform_peaks` 返回长度 == buckets 的数组,人声段桶峰值明显 > 0、静音段桶峰值接近 0。
- 对**无音轨**视频(`-an`)返回 `[]`。
- 关键:这里**不 mock ffmpeg**——后端 ffmpeg 解码可靠,正是修复点;测试必须跑真实 ffmpeg(skip 当 ffmpeg 缺失)。

```python
def extract_waveform_peaks(video_path: str, buckets: int = 200) -> list[float]:
    import subprocess, array
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", video_path,
         "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
        capture_output=True,
    )
    raw = proc.stdout
    if not raw:
        return []
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    n = len(samples)
    if n == 0:
        return []
    out: list[float] = []
    size = n / buckets
    for i in range(buckets):
        s = int(i * size)
        e = max(s + 1, int((i + 1) * size))
        peak = max((abs(x) for x in samples[s:e]), default=0)
        out.append(round(peak / 32768.0, 4))
    return out
```

端点(紧随 `video-info` 端点之后):
```python
@router.get("/projects/{project_id}/shots/{shot_id}/waveform")
async def get_shot_waveform(project_id: str, shot_id: int, session: AsyncSession = Depends(get_session)):
    from app.agents.video_trimmer import extract_waveform_peaks
    await _get_project_or_404(project_id, session)
    result = await session.execute(select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id))
    shot = result.scalar_one_or_none()
    if not shot or not shot.video_path:
        raise HTTPException(status_code=404, detail="Shot or video not found")
    try:
        peaks = extract_waveform_peaks(shot.video_path)
    except Exception:
        peaks = []
    return {"peaks": peaks}
```

### Task 8:前端改吃后端峰值 + 移除 Web Audio + 真实栈 e2e 验证

**Files:**
- Modify: `frontend-vite/src/lib/api.ts`(新增 `getWaveform`)
- Modify: `frontend-vite/src/components/WaveformTrack.tsx`(props `videoSrc`→`peaks`,删 Web Audio/AudioContext/fetch-decode)
- Modify: `frontend-vite/src/lib/waveform.ts`(删 `downsamplePeaks` + 其测试,峰值已由后端给;保留 `frameFromOffsetX`/`pixelForFrame`)
- Modify: `frontend-vite/src/components/TrimDialog.tsx`(拉 `getWaveform`,新增 `peaks` state,传给 WaveformTrack)
- Modify 测试:`WaveformTrack.test.tsx`、`TrimDialog.test.tsx`、`waveform.test.ts`、`waveform-trim.spec.ts`(取消 test1 fixme)

**Interfaces:**
- `api.getWaveform(projectId, shotId): Promise<{ peaks: number[] }>`。
- `WaveformTrackProps`:`{ peaks: number[] | null, totalFrames, endFrame, speechEndFrame, onScrub }`。
  - `peaks === null` → 加载中(显示「波形加载中…」+ 占位 canvas)。
  - `peaks` 为空数组 `[]` → 无音轨/失败,返回 `null`(降级回纯滑块)。
  - 非空 → 画柱(柱宽 = trackWidth/peaks.length;峰值已 0..1 归一化,低于 0.05 画 zinc-300 否则 blue-500),叠加静音带/黄线/红手柄(沿用现有 `pixelForFrame`/`frameFromOffsetX`)。

**测试要点:**
- `WaveformTrack.test.tsx`:不再 mock AudioContext/fetch;传 `peaks={[0.1,0.8,...]}` 断言 canvas 渲染 + fillRect 调用;`peaks={[]}` 断言返回 null(无 canvas);`onScrub` 仍以 `frameFromOffsetX` 计算帧(点击断言 `toHaveBeenCalledWith(120)`)。
- `TrimDialog.test.tsx`:mock `api.getWaveform` 返回非空 peaks,断言 `声纹波形` 渲染;移除不再需要的 AudioContext/fetch stub。
- `waveform.test.ts`:删 `downsamplePeaks` 相关用例,保留映射用例。
- **`waveform-trim.spec.ts`**:取消 test1 的 `test.fixme`(因数据来自后端 ffmpeg,不再依赖浏览器解码),mock `**/api/projects/*/shots/*/waveform` 返回非空 peaks,断言波形轨稳定渲染(不再卸载)。
- **真实栈验收(替代原 Task 6 手测)**:把共享栈切到本 worktree,跑 `npx playwright test waveform-trim.spec.ts`——两条用例都应通过,证明真实生产 MP4 上波形能渲染。

**收尾**:重跑全量单测 + tsc(基线 17)+ 重新派最终全分支审查覆盖 Task 7-8 的改动。
