# 裁剪弹窗双轨交互重设计 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `TrimDialog` 从"单波形 canvas 按像素猜裁剪/静音"重构成"视频胶片轨 + 音频波形轨"双轨，用一根贯穿两轨的裁剪线表达"裁剪同时影响视频+音频"，前段静音隔离成音频轨独立手柄，视频轨支持 hover 擦洗预览。

**Architecture:** 新增一个容器组件 `DualTrackTimeline` 拥有共享 x 轴的指针处理（裁剪线拖拽、hover 擦洗、前段静音手柄），内部堆叠两个纯展示组件 `VideoFilmstripTrack`（sprite 胶片条）与 `AudioWaveformTrack`（波形，从旧 `WaveformTrack` 重构而来、去掉自带指针逻辑）。后端新增一个 `filmstrip` 端点用一次 ffmpeg `tile` 调用产出横向 sprite。`TrimDialog` 用 `DualTrackTimeline` 替换旧 `WaveformTrack` + 冗余 slider，其余 state/保存逻辑不变。

**Tech Stack:** React + TypeScript + Vite；vitest（jsdom）单测；Playwright e2e；FastAPI + ffmpeg 后端；设计 token 用 `shots.pen` 同款 Tailwind 色板。

## Global Constraints

- 设计稿以 Pencil `design/shots.pen` frame `TrimDialog-Redesign` 为准；spec：`docs/superpowers/specs/2026-07-13-trim-dialog-dual-track-design.md`。
- **不改后端 EDL 语义**：裁剪仍只裁尾部（`trim_frames`，`POST …/trim`）、前段静音只静音音频（`audio_head_mute_frames`，`PUT …/audio-head-mute`），两端点及字段不变。
- 颜色严格用既有 token（与现 `WaveformTrack` 一致）：人声 `#3B82F6`(blue-500)、静音/已裁剪 `#D4D4D8`(zinc-300)、裁剪线 `#EF4444`(red-500)、前段静音 `#2563EB`(blue-600)、说话结束 `#F59E0B`(amber-500)、播放头 `#15803D`(green-700)、磨砂遮罩 `#F4F4F5CC`、前段静音染 `#2563EB24`。
- 前端命令一律在 `frontend-vite/` 目录下：单测 `npx vitest run <file>`，e2e `npx playwright test <file>`。`package.json` 无 `test` 脚本，直接用 `npx`。
- 后端命令一律在仓库根：`uv run --project backend pytest ...`。禁止直接 `python`/`pip`。
- **无硬编码绝对路径**：测试用 `tmp_path`/相对路径。
- **音视频改动用解码内容验证**（见 CLAUDE.md）：filmstrip 后端测试断言 sprite 的实际帧内容/尺寸，不只看文件存在；e2e 断言真实落库值（`GET /api/projects/{id}` 的 `trim_frames`/`audio_head_mute_frames`），不断言注入值。
- 每次 commit 前跑该任务的测试，绿了再提交。

## 范围外（本计划不做，显式声明）

- 弹窗内 `<video>` 预览接入 head-mute/裁剪的音频静音（现状播完整原音）——与双轨交互正交，留待后续。
- 后端裁剪不支持头部删帧——头部只能"静音"，本计划如实用"前段静音手柄"表达，不新增头部裁剪。

---

## File Structure

| 文件 | 职责 | 改动 |
|------|------|------|
| `frontend-vite/src/lib/waveform.ts` | 帧↔像素纯几何 | 追加 `frameAtClientX` |
| `frontend-vite/src/components/trim/VideoFilmstripTrack.tsx` | **新建** 视频胶片轨（纯展示：sprite + 裁掉遮罩） | 新建 |
| `frontend-vite/src/components/trim/AudioWaveformTrack.tsx` | **新建** 音频波形轨（纯展示：波形 canvas + 前段静音染/线） | 新建（从 `WaveformTrack` 重构，去掉指针逻辑） |
| `frontend-vite/src/components/trim/DualTrackTimeline.tsx` | **新建** 双轨容器：堆叠两轨 + 共享裁剪线（拖拽贯穿两轨）+ 前段静音手柄 + hover 游标；指针→帧 | 新建 |
| `frontend-vite/src/lib/api.ts` | API 客户端 | 追加 `getFilmstrip` |
| `backend/app/agents/video_trimmer.py` | ffmpeg 工具 | 追加 `extract_filmstrip_sprite` |
| `backend/app/api/pipeline.py` | 端点 | 追加 `GET …/filmstrip` |
| `frontend-vite/src/components/TrimDialog.tsx` | 弹窗 | 用 `DualTrackTimeline` 替换 `WaveformTrack` + 冗余 slider；hover→seek |
| `frontend-vite/src/components/WaveformTrack.tsx` + 其测试 | 旧单波形轨 | **删除**（末任务） |

依赖方向：`DualTrackTimeline` → `VideoFilmstripTrack` / `AudioWaveformTrack` / `lib/waveform`。`TrimDialog` → `DualTrackTimeline` + `api.getFilmstrip`。

---

### Task 1: `frameAtClientX` 几何助手

**Files:**
- Modify: `frontend-vite/src/lib/waveform.ts`
- Test: `frontend-vite/src/lib/__tests__/waveform.test.ts`（若不存在则创建）

**Interfaces:**
- Consumes: 既有 `frameFromOffsetX(offsetX, trackWidth, totalFrames)`
- Produces: `frameAtClientX(clientX: number, el: HTMLElement, totalFrames: number): number` — 由容器指针事件的 clientX + 轨道元素算出帧号（clamp 0..totalFrames）

- [ ] **Step 1: 写失败测试**

创建/追加 `frontend-vite/src/lib/__tests__/waveform.test.ts`：

```ts
import { describe, it, expect } from 'vitest'
import { frameAtClientX } from '../waveform'

function fakeEl(left: number, width: number): HTMLElement {
  return { getBoundingClientRect: () => ({ left, width, right: left + width, top: 0, bottom: 0, height: 0, x: left, y: 0, toJSON: () => ({}) }) } as unknown as HTMLElement
}

describe('frameAtClientX', () => {
  it('maps clientX within the element to a frame', () => {
    const el = fakeEl(100, 200) // track spans x=100..300
    expect(frameAtClientX(100, el, 240)).toBe(0)      // left edge
    expect(frameAtClientX(200, el, 240)).toBe(120)    // middle
    expect(frameAtClientX(300, el, 240)).toBe(240)    // right edge
  })
  it('clamps outside the element', () => {
    const el = fakeEl(100, 200)
    expect(frameAtClientX(40, el, 240)).toBe(0)
    expect(frameAtClientX(999, el, 240)).toBe(240)
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend-vite && npx vitest run src/lib/__tests__/waveform.test.ts`
Expected: FAIL —— `frameAtClientX is not a function`。

- [ ] **Step 3: 实现**

在 `frontend-vite/src/lib/waveform.ts` 末尾追加：

```ts
/** clientX + 轨道元素 → 帧号（clamp 到 0..totalFrames）。容器指针处理复用。 */
export function frameAtClientX(clientX: number, el: HTMLElement, totalFrames: number): number {
  const rect = el.getBoundingClientRect()
  return frameFromOffsetX(clientX - rect.left, rect.width, totalFrames)
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend-vite && npx vitest run src/lib/__tests__/waveform.test.ts`
Expected: PASS，2 passed。

- [ ] **Step 5: Commit**

```bash
git add frontend-vite/src/lib/waveform.ts frontend-vite/src/lib/__tests__/waveform.test.ts
git commit -m "feat(waveform): frameAtClientX 助手（容器级指针→帧）"
```

---

### Task 2: 后端 filmstrip sprite 端点

**Files:**
- Modify: `backend/app/agents/video_trimmer.py`
- Modify: `backend/app/api/pipeline.py`（在 `waveform` 端点后追加）
- Modify: `frontend-vite/src/lib/api.ts`
- Test: `backend/tests/unit/test_filmstrip_sprite.py`（新建）

**Interfaces:**
- Produces: `extract_filmstrip_sprite(video_path: str, out_path: str, *, count: int = 12, cell_width: int = 96) -> int` — 用一次 ffmpeg 从视频均匀取 `count` 帧、缩放到高 `cell_width*9/16`（保持宽 `cell_width`）、水平拼成 `count×1` sprite PNG 写入 `out_path`；返回实际帧数 `count`。
- Produces: `GET /api/projects/{project_id}/shots/{shot_id}/filmstrip?count=N` → `{ "url": str, "count": int, "cell_aspect": float }`
- Produces: `api.getFilmstrip(projectId, shotId, count?)` → `Promise<{ url: string; count: number; cell_aspect: number }>`

- [ ] **Step 1: 写失败测试（内容级）**

创建 `backend/tests/unit/test_filmstrip_sprite.py`：

```python
"""filmstrip sprite: 一次 ffmpeg 产出 count×1 横向缩略图条。内容级断言 sprite 尺寸。"""
import shutil
import subprocess
from pathlib import Path

import pytest

from app.agents.video_trimmer import extract_filmstrip_sprite

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not in PATH")


def _dims(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    w, h = out.stdout.strip().split(",")
    return int(w), int(h)


def _make_src(path: Path, frames: int = 120) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=128x72:rate=24",
         "-frames:v", str(frames), "-pix_fmt", "yuv420p", "-c:v", "libx264", str(path)],
        check=True, capture_output=True,
    )


def test_sprite_is_count_cells_wide(tmp_path):
    src = tmp_path / "src.mp4"
    out = tmp_path / "strip.png"
    _make_src(src)

    n = extract_filmstrip_sprite(str(src), str(out), count=12, cell_width=96)

    assert n == 12
    assert out.exists()
    w, h = _dims(out)
    # 12 cells × 96px wide, 16:9 cell → 54px tall
    assert w == pytest.approx(12 * 96, abs=12)
    assert h == pytest.approx(54, abs=4)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest backend/tests/unit/test_filmstrip_sprite.py -q`
Expected: FAIL —— `ImportError: cannot import name 'extract_filmstrip_sprite'`。

- [ ] **Step 3: 实现 `extract_filmstrip_sprite`**

在 `backend/app/agents/video_trimmer.py` 末尾追加（该文件已 `import subprocess`；若无则加）：

```python
def extract_filmstrip_sprite(
    video_path: str,
    out_path: str,
    *,
    count: int = 12,
    cell_width: int = 96,
) -> int:
    """Render a horizontal count×1 thumbnail sprite in ONE ffmpeg call.

    Picks `count` evenly-spaced frames across the clip, scales each to
    cell_width (16:9 height), and tiles them left-to-right into a single PNG.
    Returns count. Used by the trim dialog's video filmstrip track.
    """
    info = get_video_info(video_path)
    total = max(1, int(info["total_frames"]))
    n = max(1, min(count, total))
    cell_h = round(cell_width * 9 / 16)
    # select n evenly-spaced frames: n=total/step → pick every step-th, then cap n via tile
    step = max(1, total // n)
    vf = (
        f"select='not(mod(n\\,{step}))',"
        f"scale={cell_width}:{cell_h}:force_original_aspect_ratio=increase,"
        f"crop={cell_width}:{cell_h},"
        f"tile={n}x1"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(Path(video_path).resolve()),
         "-vf", vf, "-frames:v", "1", "-fps_mode", "vfr", str(out_path)],
        check=True, capture_output=True,
    )
    if not Path(out_path).exists():
        raise RuntimeError(f"extract_filmstrip_sprite produced no output: {out_path}")
    return n
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --project backend pytest backend/tests/unit/test_filmstrip_sprite.py -q`
Expected: PASS，1 passed。若 `h` 断言差异过大，先 ffprobe 实际尺寸核对再调 `abs` 容差，**不要**放宽到无意义。

- [ ] **Step 5: 加端点**

在 `backend/app/api/pipeline.py` 的 `get_shot_waveform` 函数之后追加：

```python
@router.get("/projects/{project_id}/shots/{shot_id}/filmstrip")
async def get_shot_filmstrip(
    project_id: str,
    shot_id: int,
    count: int = 12,
    session: AsyncSession = Depends(get_session),
):
    """Return a horizontal thumbnail sprite URL for the shot's source video."""
    from app.agents.video_trimmer import extract_filmstrip_sprite
    from app.services.storage import shot_dir, ts_uuid_name

    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot or not shot.video_path:
        raise HTTPException(status_code=404, detail="Shot or video not found")
    source = _dialog_source(project_id, shot_id, shot.video_path)
    n = max(4, min(count, 24))
    out = shot_dir(project_id, shot_id) / f"filmstrip_{ts_uuid_name('.png')}"
    try:
        actual = extract_filmstrip_sprite(source, str(out), count=n)
    except Exception:
        raise HTTPException(status_code=500, detail="filmstrip 生成失败")
    return {"url": to_media_url(str(out)), "count": actual, "cell_aspect": 16 / 9}
```

- [ ] **Step 6: 端点冒烟测试**

Run: `uv run --project backend pytest backend/tests/ -q -k "filmstrip or waveform or video_info"`
Expected: PASS（新单测 + 既有相关测试不回归）。

- [ ] **Step 7: 加 api.ts 客户端**

在 `frontend-vite/src/lib/api.ts` 的 `getWaveform` 之后追加：

```ts
// 视频缩略图胶片条 sprite（后端一次 ffmpeg tile 生成）
getFilmstrip: (projectId: string, shotId: number, count = 12): Promise<{ url: string; count: number; cell_aspect: number }> => {
  return request('GET', `/api/projects/${projectId}/shots/${shotId}/filmstrip?count=${count}`)
},
```

- [ ] **Step 8: Commit**

```bash
git add backend/app/agents/video_trimmer.py backend/app/api/pipeline.py \
        backend/tests/unit/test_filmstrip_sprite.py frontend-vite/src/lib/api.ts
git commit -m "feat(filmstrip): 一次 ffmpeg tile 产出视频缩略图 sprite 端点 + 客户端"
```

---

### Task 3: `VideoFilmstripTrack`（纯展示）

**Files:**
- Create: `frontend-vite/src/components/trim/VideoFilmstripTrack.tsx`
- Test: `frontend-vite/src/components/__tests__/VideoFilmstripTrack.test.tsx`

**Interfaces:**
- Produces: `VideoFilmstripTrack(props: { spriteUrl: string | null; trimFrac: number; height?: number })` — 渲染 `data-testid="video-track"` 的条：spriteUrl 作背景铺满；`spriteUrl` 为 null 时降级为纯色块（`data-testid="video-track-fallback"`）；`trimFrac`(0..1) 右侧用磨砂 `#F4F4F5CC` 覆盖（`data-testid="video-trim-overlay"`，宽度 `(1-trimFrac)*100%`）。纯展示、无指针逻辑。

- [ ] **Step 1: 写失败测试**

创建 `frontend-vite/src/components/__tests__/VideoFilmstripTrack.test.tsx`：

```tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { VideoFilmstripTrack } from '../trim/VideoFilmstripTrack'

describe('VideoFilmstripTrack', () => {
  it('spriteUrl 存在时用作背景，不显示降级块', () => {
    render(<VideoFilmstripTrack spriteUrl="/strip.png" trimFrac={0.8} />)
    expect(screen.getByTestId('video-track')).toBeTruthy()
    expect(screen.queryByTestId('video-track-fallback')).toBeNull()
  })
  it('spriteUrl 为 null 时降级为纯色块', () => {
    render(<VideoFilmstripTrack spriteUrl={null} trimFrac={0.8} />)
    expect(screen.getByTestId('video-track-fallback')).toBeTruthy()
  })
  it('裁掉遮罩宽度 = (1-trimFrac)', () => {
    render(<VideoFilmstripTrack spriteUrl="/strip.png" trimFrac={0.75} />)
    const ov = screen.getByTestId('video-trim-overlay') as HTMLElement
    expect(ov.style.width).toBe('25%')
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/VideoFilmstripTrack.test.tsx`
Expected: FAIL —— 模块不存在。

- [ ] **Step 3: 实现**

创建 `frontend-vite/src/components/trim/VideoFilmstripTrack.tsx`：

```tsx
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/VideoFilmstripTrack.test.tsx`
Expected: PASS，3 passed。

- [ ] **Step 5: Commit**

```bash
git add frontend-vite/src/components/trim/VideoFilmstripTrack.tsx frontend-vite/src/components/__tests__/VideoFilmstripTrack.test.tsx
git commit -m "feat(trim): VideoFilmstripTrack 视频胶片轨（sprite + 裁掉遮罩）"
```

---

### Task 4: `AudioWaveformTrack`（纯展示，从 WaveformTrack 重构画法）

**Files:**
- Create: `frontend-vite/src/components/trim/AudioWaveformTrack.tsx`
- Test: `frontend-vite/src/components/__tests__/AudioWaveformTrack.test.tsx`

**Interfaces:**
- Produces: `AudioWaveformTrack(props: { peaks: number[] | null; trimFrac: number; headMuteFrac: number; speechEndFrac: number | null; height?: number })` — canvas 画波形（蓝 `#3B82F6` 人声 / 灰 `#D4D4D8` 静音），右侧 `trimFrac` 后磨砂灰显，左侧 `headMuteFrac` 前淡蓝染 `#2563EB24`，`speechEndFrac` 处黄线画在灰显之上。`data-testid="audio-track"`。纯展示、无指针逻辑（指针由容器负责）。`peaks===null` 显示加载态，`peaks.length===0` 仍渲染空 canvas（无音频）。

- [ ] **Step 1: 写失败测试**

创建 `frontend-vite/src/components/__tests__/AudioWaveformTrack.test.tsx`（复用旧 WaveformTrack 测试的 canvas ctx 打桩方式）：

```tsx
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { AudioWaveformTrack } from '../trim/AudioWaveformTrack'

let fillStyleLog: string[] = []
beforeEach(() => {
  fillStyleLog = []
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => {
    const ctx: any = { fillRect: () => fillStyleLog.push(ctx.fillStyle), clearRect: () => {}, fillStyle: '' }
    return ctx
  })
})
afterEach(() => vi.restoreAllMocks())

describe('AudioWaveformTrack', () => {
  it('非空 peaks 渲染 canvas 并画条', () => {
    render(<AudioWaveformTrack peaks={[0.1, 0.8, 0.5, 0.9]} trimFrac={1} headMuteFrac={0} speechEndFrac={null} />)
    expect(screen.getByTestId('audio-track')).toBeTruthy()
    expect(fillStyleLog.some((c) => c.toUpperCase() === '#3B82F6')).toBe(true)
  })
  it('headMuteFrac>0 画淡蓝前段染', () => {
    render(<AudioWaveformTrack peaks={[0.5, 0.6]} trimFrac={1} headMuteFrac={0.2} speechEndFrac={null} />)
    expect(fillStyleLog.some((c) => c.toUpperCase().startsWith('#2563EB'))).toBe(true)
  })
  it('peaks 为 null 显示加载态', () => {
    render(<AudioWaveformTrack peaks={null} trimFrac={1} headMuteFrac={0} speechEndFrac={null} />)
    expect(screen.getByTestId('audio-track')).toBeTruthy()
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/AudioWaveformTrack.test.tsx`
Expected: FAIL —— 模块不存在。

- [ ] **Step 3: 实现**

创建 `frontend-vite/src/components/trim/AudioWaveformTrack.tsx`。把旧 `WaveformTrack.tsx` 的 canvas 绘制逻辑搬过来，**去掉所有指针/scrub 回调**，改成按 `*Frac`(0..1) 而非帧号绘制。绘制顺序：波形条 → 右侧磨砂灰显 → 左侧前段染 → 黄线（在灰显之上）：

```tsx
import { useEffect, useRef } from 'react'

interface Props {
  peaks: number[] | null
  trimFrac: number
  headMuteFrac: number
  speechEndFrac: number | null
  height?: number
}

const VOICED = '#3B82F6'
const SILENCE = '#D4D4D8'
const MUTE_TINT = '#2563EB24'
const FROST = '#F4F4F5CC'
const AMBER = '#F59E0B'

/** 音频波形轨（纯展示）：波形 + 裁掉灰显 + 前段静音染 + 说话结束黄线。指针由容器负责。 */
export function AudioWaveformTrack({ peaks, trimFrac, headMuteFrac, speechEndFrac, height = 84 }: Props) {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const cv = ref.current
    if (!cv || !peaks) return
    const w = cv.offsetWidth || 500
    const h = height
    cv.width = w
    cv.height = h
    const ctx = cv.getContext('2d')
    if (!ctx) return
    ctx.clearRect(0, 0, w, h)
    const n = peaks.length
    const bw = n > 0 ? w / n : w
    for (let i = 0; i < n; i++) {
      const v = peaks[i]
      const bh = Math.max(2, v * (h - 12))
      ctx.fillStyle = v < 0.05 ? SILENCE : VOICED
      ctx.fillRect(i * bw + bw * 0.15, (h - bh) / 2, Math.max(1, bw * 0.7), bh)
    }
    // 右侧裁掉灰显
    const trimX = trimFrac * w
    ctx.fillStyle = FROST
    ctx.fillRect(trimX, 0, w - trimX, h)
    // 左侧前段静音染
    if (headMuteFrac > 0) {
      ctx.fillStyle = MUTE_TINT
      ctx.fillRect(0, 0, headMuteFrac * w, h)
    }
    // 说话结束黄线（画在灰显之上）
    if (speechEndFrac != null) {
      ctx.fillStyle = AMBER
      ctx.fillRect(speechEndFrac * w - 1, 0, 2, h)
    }
  }, [peaks, trimFrac, headMuteFrac, speechEndFrac, height])

  return (
    <div data-testid="audio-track" className="relative rounded-md bg-zinc-50 border border-zinc-200 overflow-hidden" style={{ height }}>
      {peaks === null ? (
        <div className="absolute inset-0 flex items-center justify-center text-[11px] text-zinc-400">加载波形…</div>
      ) : (
        <canvas ref={ref} className="w-full block" style={{ height }} />
      )}
    </div>
  )
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/AudioWaveformTrack.test.tsx`
Expected: PASS，3 passed。

- [ ] **Step 5: Commit**

```bash
git add frontend-vite/src/components/trim/AudioWaveformTrack.tsx frontend-vite/src/components/__tests__/AudioWaveformTrack.test.tsx
git commit -m "feat(trim): AudioWaveformTrack 音频波形轨（波形+灰显+前段染+黄线）"
```

---

### Task 5: `DualTrackTimeline` 容器（共享裁剪线 + 前段静音手柄 + hover）

**Files:**
- Create: `frontend-vite/src/components/trim/DualTrackTimeline.tsx`
- Test: `frontend-vite/src/components/__tests__/DualTrackTimeline.test.tsx`

**Interfaces:**
- Consumes: `VideoFilmstripTrack`(Task 3)、`AudioWaveformTrack`(Task 4)、`frameAtClientX`(Task 1)
- Produces: `DualTrackTimeline(props: { spriteUrl: string | null; peaks: number[] | null; totalFrames: number; endFrame: number; headMuteFrame: number; speechEndFrame: number | null; playheadFrame: number | null; onTrimChange: (frame: number) => void; onHeadMuteChange: (frame: number) => void; onHoverFrame: (frame: number | null) => void })`。渲染堆叠两轨；一根 `data-testid="cut-line"` 红线贯穿两轨（拖拽=改 endFrame）；音频轨上 `data-testid="headmute-handle"` 蓝手柄（拖拽=改 headMuteFrame）；hover 视频轨触发 `onHoverFrame`；离开触发 `onHoverFrame(null)`。指针→帧用 `frameAtClientX`。

**关键决策（消歧的落点）**：裁剪线的抓取热区在两轨中缝（跨视频/音频），前段静音手柄的热区只在音频轨左端。两者热区不重叠、颜色不同、testid 不同——不再靠像素猜落到哪个操作。

- [ ] **Step 1: 写失败测试**

创建 `frontend-vite/src/components/__tests__/DualTrackTimeline.test.tsx`：

```tsx
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { DualTrackTimeline } from '../trim/DualTrackTimeline'

beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => {
    const ctx: any = { fillRect: () => {}, clearRect: () => {}, fillStyle: '' }
    return ctx
  })
  // 容器测宽 400px，从 x=0 开始
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    left: 0, width: 400, right: 400, top: 0, bottom: 100, height: 100, x: 0, y: 0, toJSON: () => ({}),
  } as DOMRect)
})
afterEach(() => vi.restoreAllMocks())

function base() {
  return {
    spriteUrl: '/s.png', peaks: [0.5, 0.6, 0.7], totalFrames: 240, endFrame: 240,
    headMuteFrame: 0, speechEndFrame: null, playheadFrame: null,
    onTrimChange: vi.fn(), onHeadMuteChange: vi.fn(), onHoverFrame: vi.fn(),
  }
}

describe('DualTrackTimeline', () => {
  it('渲染视频轨 + 音频轨 + 裁剪线', () => {
    render(<DualTrackTimeline {...base()} />)
    expect(screen.getByTestId('video-track')).toBeTruthy()
    expect(screen.getByTestId('audio-track')).toBeTruthy()
    expect(screen.getByTestId('cut-line')).toBeTruthy()
  })
  it('拖裁剪线上报新裁剪帧（x=200/400 → 120 帧）', () => {
    const p = base()
    render(<DualTrackTimeline {...p} />)
    const line = screen.getByTestId('cut-line')
    fireEvent.pointerDown(line, { clientX: 200 })
    fireEvent.pointerMove(line, { clientX: 200 })
    expect(p.onTrimChange).toHaveBeenCalledWith(120)
  })
  it('拖前段静音手柄上报静音帧', () => {
    const p = { ...base(), headMuteFrame: 24 }
    render(<DualTrackTimeline {...p} />)
    const handle = screen.getByTestId('headmute-handle')
    fireEvent.pointerDown(handle, { clientX: 40 })
    fireEvent.pointerMove(handle, { clientX: 40 })
    expect(p.onHeadMuteChange).toHaveBeenCalledWith(24) // 40/400*240=24
  })
  it('hover 视频轨上报 hover 帧，离开上报 null', () => {
    const p = base()
    render(<DualTrackTimeline {...p} />)
    const hover = screen.getByTestId('video-hover') // hover 包裹（onPointerLeave 不冒泡，须直接命中它）
    fireEvent.pointerMove(hover, { clientX: 100 })
    expect(p.onHoverFrame).toHaveBeenCalledWith(60) // 100/400*240
    fireEvent.pointerLeave(hover)
    expect(p.onHoverFrame).toHaveBeenCalledWith(null)
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/DualTrackTimeline.test.tsx`
Expected: FAIL —— 模块不存在。

- [ ] **Step 3: 实现**

创建 `frontend-vite/src/components/trim/DualTrackTimeline.tsx`。容器 `layout` 用一个定位 wrapper（`position:relative`），两轨纵向堆叠，裁剪线/手柄/游标绝对定位覆盖。指针拖拽用 `draggingRef` 门控（pointerDown 置真、pointerUp 置假），pointerMove 只在拖拽中生效：

```tsx
import { useCallback, useRef } from 'react'
import { VideoFilmstripTrack } from './VideoFilmstripTrack'
import { AudioWaveformTrack } from './AudioWaveformTrack'
import { frameAtClientX } from '@/lib/waveform'

interface Props {
  spriteUrl: string | null
  peaks: number[] | null
  totalFrames: number
  endFrame: number
  headMuteFrame: number
  speechEndFrame: number | null
  playheadFrame: number | null
  onTrimChange: (frame: number) => void
  onHeadMuteChange: (frame: number) => void
  onHoverFrame: (frame: number | null) => void
}

const V_H = 60
const GAP = 12
const A_H = 84

export function DualTrackTimeline({
  spriteUrl, peaks, totalFrames, endFrame, headMuteFrame, speechEndFrame, playheadFrame,
  onTrimChange, onHeadMuteChange, onHoverFrame,
}: Props) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<null | 'trim' | 'mute'>(null)

  const frac = (f: number) => (totalFrames > 0 ? Math.min(1, Math.max(0, f / totalFrames)) : 0)

  const startDrag = useCallback((kind: 'trim' | 'mute') => (e: React.PointerEvent) => {
    dragRef.current = kind
    e.currentTarget.setPointerCapture?.(e.pointerId)
    const f = wrapRef.current ? frameAtClientX(e.clientX, wrapRef.current, totalFrames) : 0
    if (kind === 'trim') onTrimChange(f)
    else onHeadMuteChange(Math.max(0, Math.min(f, totalFrames)))
  }, [totalFrames, onTrimChange, onHeadMuteChange])

  const onMove = useCallback((e: React.PointerEvent) => {
    if (!dragRef.current || !wrapRef.current) return
    const f = frameAtClientX(e.clientX, wrapRef.current, totalFrames)
    if (dragRef.current === 'trim') onTrimChange(f)
    else onHeadMuteChange(Math.max(0, Math.min(f, totalFrames)))
  }, [totalFrames, onTrimChange, onHeadMuteChange])

  const endDrag = useCallback(() => { dragRef.current = null }, [])

  const hoverMove = useCallback((e: React.PointerEvent) => {
    if (dragRef.current || !wrapRef.current) return
    onHoverFrame(frameAtClientX(e.clientX, wrapRef.current, totalFrames))
  }, [totalFrames, onHoverFrame])

  const cutLeft = `${frac(endFrame) * 100}%`
  const muteLeft = `${frac(headMuteFrame) * 100}%`
  const totalH = V_H + GAP + A_H

  return (
    <div ref={wrapRef} className="relative select-none" style={{ height: totalH }} onPointerMove={onMove} onPointerUp={endDrag} onPointerCancel={endDrag}>
      <div data-testid="video-hover" style={{ height: V_H }} onPointerMove={hoverMove} onPointerLeave={() => onHoverFrame(null)}>
        <VideoFilmstripTrack spriteUrl={spriteUrl} trimFrac={frac(endFrame)} height={V_H} />
      </div>
      <div style={{ height: GAP }} />
      <div style={{ height: A_H }} className="relative">
        <AudioWaveformTrack peaks={peaks} trimFrac={frac(endFrame)} headMuteFrac={frac(headMuteFrame)} speechEndFrac={speechEndFrame != null ? frac(speechEndFrame) : null} height={A_H} />
        {/* 前段静音蓝手柄：只在音频轨 */}
        <div data-testid="headmute-handle" role="slider" aria-label="前段静音"
          onPointerDown={startDrag('mute')} onPointerMove={onMove} onPointerUp={endDrag}
          className="absolute top-0 h-full cursor-ew-resize" style={{ left: muteLeft, width: 16, transform: 'translateX(-8px)' }}>
          <div className="absolute inset-y-0" style={{ left: 7, width: 2.5, background: '#2563EB' }} />
          <div className="absolute" style={{ top: '38%', left: 2, width: 12, height: 40, borderRadius: 6, background: '#2563EB' }} />
        </div>
      </div>
      {/* 裁剪线：贯穿两轨；抓手在中缝 */}
      <div data-testid="cut-line" role="slider" aria-label="裁剪"
        onPointerDown={startDrag('trim')} onPointerMove={onMove} onPointerUp={endDrag}
        className="absolute top-0 cursor-ew-resize" style={{ left: cutLeft, height: totalH, width: 16, transform: 'translateX(-8px)' }}>
        <div className="absolute inset-y-0" style={{ left: 7, width: 3, background: '#EF4444' }} />
        <div className="absolute" style={{ top: V_H + GAP / 2 - 30, left: 1, width: 15, height: 60, borderRadius: 7, background: '#EF4444' }} />
      </div>
      {/* 播放头（预览时贯穿两轨） */}
      {playheadFrame != null && (
        <div data-testid="playhead" className="absolute top-0 pointer-events-none" style={{ left: `${frac(playheadFrame) * 100}%`, height: totalH, width: 2, background: '#15803D' }} />
      )}
    </div>
  )
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/DualTrackTimeline.test.tsx`
Expected: PASS，4 passed。若 hover 测试因 pointer-capture 桩缺失报错，在 `beforeEach` 补 `HTMLElement.prototype.setPointerCapture = () => {}`。

- [ ] **Step 5: Commit**

```bash
git add frontend-vite/src/components/trim/DualTrackTimeline.tsx frontend-vite/src/components/__tests__/DualTrackTimeline.test.tsx
git commit -m "feat(trim): DualTrackTimeline 双轨容器（贯穿裁剪线+前段静音手柄+hover擦洗）"
```

---

### Task 6: 接入 `TrimDialog`

**Files:**
- Modify: `frontend-vite/src/components/TrimDialog.tsx`
- Test: `frontend-vite/src/components/__tests__/TrimDialog.test.tsx`（既有，需补/改）

**Interfaces:**
- Consumes: `DualTrackTimeline`(Task 5)、`api.getFilmstrip`(Task 2)
- Produces: TrimDialog 用 `DualTrackTimeline` 替换 `<WaveformTrack>`（`TrimDialog.tsx:345-354`）与其下方冗余的蓝/灰 slider 块（`TrimDialog.tsx:358-378`，见现状分析）；新增 `spriteUrl` state + 载入时 `api.getFilmstrip`；hover 帧 → `seekToFrame`。保存逻辑 `handleTrim`、其余 state 不变。

- [ ] **Step 1: 写失败测试**

在 `frontend-vite/src/components/__tests__/TrimDialog.test.tsx` 的 `vi.mock('@/lib/api', ...)` 里给 mock 追加 `getFilmstrip: vi.fn().mockResolvedValue({ url: '/strip.png', count: 12, cell_aspect: 16/9 })`，并追加用例：

```tsx
it('渲染双轨（视频轨 + 音频轨），不再有旧冗余裁剪 slider', async () => {
  renderReady()
  expect(await screen.findByTestId('video-track')).toBeTruthy()
  expect(screen.getByTestId('audio-track')).toBeTruthy()
  expect(screen.getByTestId('cut-line')).toBeTruthy()
  // 旧的 range slider 不再存在
  expect(document.querySelector('input[type="range"]')).toBeNull()
})

it('hover 视频轨把预览 video seek 到该帧', async () => {
  renderReady()
  const vt = await screen.findByTestId('video-track')
  const video = document.querySelector('video') as HTMLVideoElement
  // fps=24；hover 到 60 帧应 seek 到 2.5s（用容器 rect 桩，见 DualTrackTimeline 测试同法）
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ left: 0, width: 240, right: 240, top: 0, bottom: 0, height: 0, x: 0, y: 0, toJSON: () => ({}) } as DOMRect)
  fireEvent.pointerMove(vt, { clientX: 60 })
  expect(video.currentTime).toBeCloseTo(60 / 24, 1)
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/TrimDialog.test.tsx -t "双轨"`
Expected: FAIL —— 无 `video-track`（仍是旧 WaveformTrack）。

- [ ] **Step 3: 实现接入**

在 `TrimDialog.tsx`：
1. import：`import { DualTrackTimeline } from './trim/DualTrackTimeline'`，删除 `import WaveformTrack from './WaveformTrack'`。
2. state：加 `const [spriteUrl, setSpriteUrl] = useState<string | null>(null)`。
3. 载入 effect（与现有 `getWaveform`/`getVideoInfo` 载入同处）追加：
```tsx
api.getFilmstrip(projectId, shot.shot_id).then((r) => setSpriteUrl(r.url)).catch(() => setSpriteUrl(null))
```
4. 把 `<WaveformTrack .../>`（345-354 行）**和**其下方冗余 slider 块（358-378 行的 `<div className="relative h-3 …">` + `<input type="range" …>`）整体替换为：
```tsx
<DualTrackTimeline
  spriteUrl={spriteUrl}
  peaks={peaks}
  totalFrames={totalFrames}
  endFrame={endFrame}
  headMuteFrame={headMuteFrame}
  speechEndFrame={speechEndFrame}
  playheadFrame={playheadFrame}
  onTrimChange={handleSliderChange}
  onHeadMuteChange={(f) => setHeadMuteFrame(Math.max(0, Math.min(f, totalFrames)))}
  onHoverFrame={(f) => { if (f != null && !isPreviewing) seekToFrame(f) }}
/>
```
（`handleSliderChange` 是现有的裁剪点变更处理器，见现状分析中它同时 `setEndFrame` + `seekToFrame`；保持复用。若其签名是 `(frame:number)=>void` 直接传即可。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/TrimDialog.test.tsx`
Expected: PASS。既有 21 个用例中凡断言旧 slider/WaveformTrack 具体 DOM 的，按新结构更新断言（例如"加载后渲染声纹波形轨"改为断言 `audio-track`）。**不要**删除断言语义，只更新选择器。

- [ ] **Step 5: 跑整个前端单测确认无回归**

Run: `cd frontend-vite && npx vitest run`
Expected: 全绿（e2e 的 `*.spec.ts` 被 vitest 收进来的既有 Playwright 报错除外——那是既有配置噪音，与本改动无关）。

- [ ] **Step 6: Commit**

```bash
git add frontend-vite/src/components/TrimDialog.tsx frontend-vite/src/components/__tests__/TrimDialog.test.tsx
git commit -m "feat(trim): TrimDialog 接入 DualTrackTimeline，移除冗余裁剪 slider + hover 擦洗预览"
```

---

### Task 7: e2e —— 真实后端播种的双轨交互

**Files:**
- Create: `frontend-vite/e2e/trim-dual-track.spec.ts`

**Interfaces:**
- Consumes: 真实运行栈（backend :8002 + frontend :4000）
- Produces: 一个 e2e：真实 DB 播种一个带视频的 completed shot（复用后端已生成资产，绝不调模型），打开裁剪弹窗，拖裁剪线，断言 `POST …/trim` 后真实 `GET /api/projects/{id}` 的 `trim_frames` 变化。

**CLAUDE.md 合规**：既有 `waveform-trim.spec.ts` 用 `route.fulfill` 伪造 `GET /api/projects/:id`，被 CLAUDE.md 判为 fake-e2e。本新 e2e **必须**走真实后端/DB：用 `podman exec` 往真实 DB 插入 shot 行 + 复制一个已生成的 `output_*.mp4` 到隔离测试项目的 shot 目录（参照 CLAUDE.md「E2E Tests」示例）。只短路会计费的 AI 端点（本用例不触发）。

- [ ] **Step 1: 写 e2e（先让它跑起来看真实交互）**

创建 `frontend-vite/e2e/trim-dual-track.spec.ts`：

```ts
import { test, expect } from '@playwright/test'

const USER = 'e2e-dual'
const PID = 'e2e-dual-track'

test.describe('裁剪弹窗 · 双轨联动', () => {
  test.beforeAll(async () => {
    // 真实播种：往真实 DB 插项目+shot，复制一个已生成视频到 shot 目录。
    // 用 podman exec 进 backend 容器执行 seed 脚本（参照 CLAUDE.md E2E 示例）。
    // 具体 seed 命令在实现时按当前容器名/路径填入，产出：
    //   project PID (status=shot_review) + shot 1 (status=completed, 真实 video_path, source_fps/frames)
  })
  test.afterAll(async () => {
    // 删除测试项目行 + shot 目录
  })

  test('拖裁剪线 → 真实 trim_frames 落库变化', async ({ page }) => {
    await page.goto(`/projects/${PID}/shots`)
    await page.getByTestId('shot-card-1').getByRole('button', { name: '裁剪' }).click()
    await expect(page.getByTestId('video-track')).toBeVisible()
    await expect(page.getByTestId('audio-track')).toBeVisible()
    const line = page.getByTestId('cut-line')
    const box = await line.boundingBox()
    // 往左拖到 ~60% 处
    const track = await page.getByTestId('audio-track').boundingBox()
    await line.hover()
    await page.mouse.down()
    await page.mouse.move(track!.x + track!.width * 0.6, box!.y + box!.height / 2)
    await page.mouse.up()
    await page.getByRole('button', { name: '确认裁剪' }).click()
    // 断言真实落库：GET 项目，trim_frames 应被写入且 < source_frames
    const resp = await page.request.get(`/api/projects/${PID}`, { headers: { 'X-User-Name': USER } })
    const proj = await resp.json()
    const shot = proj.shots.find((s: any) => s.shot_id === 1)
    expect(shot.trim_frames).toBeGreaterThan(0)
    expect(shot.trim_frames).toBeLessThan(shot.source_frames)
  })
})
```

- [ ] **Step 2: 填 seed/teardown 并跑**

按 CLAUDE.md「E2E Tests」的 `podman exec` 播种范式，在 `beforeAll`/`afterAll` 填入真实 seed（插 DB 行 + 复制已生成 `output_*.mp4` 到隔离测试项目 shot 目录）与清理。确认栈在跑（backend :8002 / frontend :4000）。

Run: `cd frontend-vite && npx playwright test e2e/trim-dual-track.spec.ts`
Expected: PASS，1 passed。

- [ ] **Step 3: Commit**

```bash
git add frontend-vite/e2e/trim-dual-track.spec.ts
git commit -m "test(e2e): 双轨裁剪线拖拽 → 真实 trim_frames 落库（真实后端播种）"
```

---

### Task 8: 删除旧 `WaveformTrack` 与冗余残留

**Files:**
- Delete: `frontend-vite/src/components/WaveformTrack.tsx`
- Delete: `frontend-vite/src/components/__tests__/WaveformTrack.test.tsx`
- Modify: 任何仍 import 旧 `WaveformTrack` 的文件

**Interfaces:**
- Consumes: 前序任务已把唯一使用方 `TrimDialog` 切到 `DualTrackTimeline`

- [ ] **Step 1: 确认无残留引用**

Run: `cd frontend-vite && grep -rn "WaveformTrack" src | grep -v "AudioWaveformTrack"`
Expected: 仅剩 `WaveformTrack.tsx` 自身与其测试（即将删）。若 `TrimDialog` 仍有引用，回 Task 6 修。

- [ ] **Step 2: 删除**

```bash
git rm frontend-vite/src/components/WaveformTrack.tsx frontend-vite/src/components/__tests__/WaveformTrack.test.tsx
```

- [ ] **Step 3: 跑全量前端单测**

Run: `cd frontend-vite && npx vitest run`
Expected: 全绿（无 `Cannot find module '../WaveformTrack'`）。

- [ ] **Step 4: Commit**

```bash
git commit -m "chore(trim): 删除旧 WaveformTrack（已被 DualTrackTimeline 取代）"
```

---

## Self-Review

**Spec 覆盖检查**

| Spec 要求 | 对应任务 |
|-----------|----------|
| 视频轨=缩略图胶片条 | Task 2（sprite 后端）+ Task 3（VideoFilmstripTrack） |
| 音频轨=波形，裁掉灰显、前段染 | Task 4（AudioWaveformTrack） |
| 一根裁剪线贯穿两轨（联动） | Task 5（DualTrackTimeline cut-line 贯穿 totalH） |
| 前段静音=音频轨独立手柄 | Task 5（headmute-handle 只在音频轨区） |
| hover 视频轨→预览器 seek | Task 5（onHoverFrame）+ Task 6（seekToFrame 接线） |
| 说话结束黄线/播放头绿线只读 | Task 4（黄线）+ Task 5（playhead，pointer-events:none） |
| 删除冗余蓝/灰 slider | Task 6（替换时删除 358-378 块）+ Task 8 |
| 不改后端 EDL 语义 | 全程只读 `video-info`/`waveform` + 复用 `trimShot`/`setAudioHeadMute` |
| 内容级验证 | Task 2（sprite 尺寸 ffprobe）；Task 7（真实落库断言） |
| e2e 走真实后端 | Task 7（podman exec 真实播种，显式不用 route.fulfill 伪造项目） |

**类型一致性**：`VideoFilmstripTrack({spriteUrl,trimFrac,height})`、`AudioWaveformTrack({peaks,trimFrac,headMuteFrac,speechEndFrac,height})`、`DualTrackTimeline({...,onTrimChange,onHeadMuteChange,onHoverFrame})`、`frameAtClientX(clientX,el,totalFrames)`、`api.getFilmstrip(projectId,shotId,count?)→{url,count,cell_aspect}`、`extract_filmstrip_sprite(video_path,out_path,*,count,cell_width)→int`——各任务定义与调用处签名一致。`DualTrackTimeline` 用 `frac()` 把帧号转 0..1 再传给两个纯展示轨（它们的 props 是 `*Frac`），一致。

**占位扫描**：无 TBD/TODO。Task 7 的 seed 命令是"按当前容器名/路径填入"——这是 e2e 真实播种的固有环境依赖（容器名运行时才确定），已在步骤里明确指向 CLAUDE.md 的 `podman exec` 范式，不是模糊需求。

**已知取舍**：filmstrip 失败时 `VideoFilmstripTrack` 降级为纯色块（Task 3），使前端 Task 3-6 不被后端 Task 2 阻塞；后端可独立推进。
