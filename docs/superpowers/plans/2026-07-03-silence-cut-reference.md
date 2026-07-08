# 静音帧剪切参考 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在裁剪弹窗 `TrimDialog` 里，把尾部静音以「帧数剪切参考」显性呈现（尾部静音 N 帧 / 建议第 X 帧 / 总帧数），并提供「采用参考」一键把裁剪点吸附到参考帧。

**Architecture:** 纯前端展示层改动。参考帧复用弹窗打开时 `GET /video-info` 已返回的 `speech_end_frame`（后端内部已跑静音检测算出），无需新请求、无后端改动。可裁静音帧数在前端推导：`silenceFrames = totalFrames - speechEndFrame`。

**Tech Stack:** React + TypeScript（Vite），vitest + @testing-library/react，lucide-react 图标，Tailwind。

## Global Constraints

- 禁止硬编码绝对路径（用相对/`__dirname`）。
- 前端测试用 vitest（`./node_modules/.bin/vitest run`），组件测试可在 `@/lib/api` 边界打桩（这是既有模式，非 e2e）；Playwright e2e 必须打真实后端，本计划不新增 e2e。
- 不改静音检测算法、不改 `/trim` 执行、不改 `WaveformTrack`。
- 不改后端（不新增 `detect-silence` 字段）。
- TypeScript 严格：不得引入未定义字段；`Shot` 类型不含 `first_frame_path`（已删）。

## 计划期决策（相对已批 spec 的偏离，需实现前知悉）

1. **参考帧来源改为 `getVideoInfo().speech_end_frame`（原始话尾帧），而非 `detect-silence` 的 `suggested_end_frame`（+3 呼吸帧）。** 原因：`video-info` 打开时已返回该帧且波形参考线已在用它，零额外请求、三者（波形线/读数/采用参考落点）完全一致。代价：放弃 +3 呼吸帧（用户可用步进按钮自行加）。spec 数据源章节已认可「取已加载者即可，二者语义一致」。
2. **移除现有冗余的「静音裁剪」按钮**（`handleDetectSilence` + `isDetectingSilence` + `api.detectSilence` 在本组件的用法）。新「采用参考」按钮取代它，且不发网络请求。`detect-silence` 端点保留（不删后端）。
3. mockup 中的「🎧 重新检测静音」按钮**不实现**——参考帧随弹窗打开即加载，无需重新检测。

## File Structure

- **Modify** `frontend-vite/src/components/TrimDialog.tsx`
  - 新增派生值 `silenceFrames` 与 `showSilenceRef`。
  - 预览区与波形区之间新增「静音剪切参考条」+ 三格帧数读数。
  - 新增 `handleApplyReference()`。
  - 移除 `handleDetectSilence` / `isDetectingSilence` / 「静音裁剪」按钮 / `api.detectSilence` 引用。
- **Modify** `frontend-vite/src/components/__tests__/TrimDialog.test.tsx`
  - 复用现有 mock（`getVideoInfo` 已返回 `speech_end_frame: 180, total_frames: 240` → `silenceFrames = 60`）。
  - 新增：参考条渲染、无静音隐藏、采用参考落点三组断言。

无后端文件改动。

---

### Task 1: 静音剪切参考条 + 帧数读数（渲染）

**Files:**
- Modify: `frontend-vite/src/components/TrimDialog.tsx`（派生值 + 预览与波形之间插入参考条；约 258 行 `const currentTime...` 附近加派生值，约 284–287 行之间插入 JSX）
- Test: `frontend-vite/src/components/__tests__/TrimDialog.test.tsx`

**Interfaces:**
- Consumes: 组件已有 state `speechEndFrame: number | null`（第 43 行，`getVideoInfo().speech_end_frame` 于第 139 行赋值）、`totalFrames: number`、`fps: number`。
- Produces: 派生常量 `silenceFrames: number`、`showSilenceRef: boolean`；DOM 中出现文本 `尾部静音 {silenceFrames} 帧`、`建议剪到第 {speechEndFrame} 帧`，及读数 `可裁静音 {silenceFrames} 帧`。供 Task 2 的「采用参考」按钮同区渲染。

- [ ] **Step 1: 写失败测试（参考条按帧数渲染）**

在 `TrimDialog.test.tsx` 的 `describe` 内新增（现有 mock：`speech_end_frame:180`，`total_frames:240` → 静音 60 帧）：

```tsx
  it('尾部静音时显示帧数剪切参考(尾部静音帧数 / 建议帧 / 可裁帧)', async () => {
    await renderReady()
    // 参考条：尾部静音 60 帧 · 建议剪到第 180 帧
    expect(screen.getByText(/尾部静音\s*60\s*帧/)).toBeInTheDocument()
    expect(screen.getByText(/建议剪到第\s*180\s*帧/)).toBeInTheDocument()
    // 读数：可裁静音 60 帧
    expect(screen.getByText(/可裁静音\s*60\s*帧/)).toBeInTheDocument()
  })

  it('无尾部静音时不显示参考条', async () => {
    ;(api.getVideoInfo as any).mockResolvedValueOnce({
      fps: 24, total_frames: 240, duration: 10.0, has_backup: false,
      speech_end_frame: null, speech_end_sec: null,
    })
    await renderReady()
    expect(screen.queryByText(/尾部静音/)).not.toBeInTheDocument()
    expect(screen.queryByText(/可裁静音/)).not.toBeInTheDocument()
  })
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend-vite && ./node_modules/.bin/vitest run src/components/__tests__/TrimDialog.test.tsx -t "剪切参考"`
Expected: FAIL —「Unable to find an element with text: /尾部静音 60 帧/」。

（注：若 worktree 无 `node_modules`，先 `ln -s ../../../frontend-vite/node_modules node_modules` 借用主 checkout 依赖，收尾时 `rm -f node_modules`。）

- [ ] **Step 3: 加派生值**

在 `TrimDialog.tsx` 第 258 行 `const currentTime = ...` 之前插入：

```tsx
  // 静音剪切参考：话尾帧之后到片尾都是尾部静音，可作为裁剪依据（帧数）。
  // speechEndFrame 来自 getVideoInfo（后端已算），零额外请求。
  const silenceFrames =
    speechEndFrame != null ? totalFrames - speechEndFrame : 0
  const showSilenceRef = speechEndFrame != null && silenceFrames > 0
```

- [ ] **Step 4: 插入参考条 + 读数 JSX**

在预览区块（`</div>` 结束于第 284 行）与波形区块（第 287 行 `{/* Waveform track ... */}`）之间插入：

```tsx
            {/* 静音剪切参考（尾部静音帧数）*/}
            {showSilenceRef && (
              <div className="shrink-0 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2">
                <div className="flex items-center gap-3">
                  <span className="text-lg leading-none">🔇</span>
                  <div className="flex-1 text-sm text-amber-200">
                    检测到尾部静音 <b className="text-amber-400">{silenceFrames} 帧</b>
                    {fps > 0 && (
                      <span className="text-amber-300/70">
                        {' '}({(silenceFrames / fps).toFixed(2)}s)
                      </span>
                    )}
                    {' '}· 建议剪到第 <b className="text-amber-400">{speechEndFrame} 帧</b>
                  </div>
                </div>
                <div className="mt-2 grid grid-cols-3 gap-2 text-xs">
                  <div className="rounded bg-black/20 px-2 py-1">
                    <div className="text-zinc-400">总帧数</div>
                    <div className="font-semibold">{totalFrames}</div>
                  </div>
                  <div className="rounded bg-black/20 px-2 py-1">
                    <div className="text-zinc-400">建议剪切点</div>
                    <div className="font-semibold text-amber-400">{speechEndFrame} 帧</div>
                  </div>
                  <div className="rounded bg-black/20 px-2 py-1">
                    <div className="text-zinc-400">可裁静音</div>
                    <div className="font-semibold text-amber-400">{silenceFrames} 帧</div>
                  </div>
                </div>
              </div>
            )}
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd frontend-vite && ./node_modules/.bin/vitest run src/components/__tests__/TrimDialog.test.tsx`
Expected: PASS（新增 2 项 + 原有全部）。

- [ ] **Step 6: 提交**

```bash
git add frontend-vite/src/components/TrimDialog.tsx frontend-vite/src/components/__tests__/TrimDialog.test.tsx
git commit -m "feat(trim): show trailing-silence cut reference (frame counts) in TrimDialog"
```

---

### Task 2: 「采用参考」一键吸附裁剪点

**Files:**
- Modify: `frontend-vite/src/components/TrimDialog.tsx`（新增 `handleApplyReference`；在 Task 1 的参考条内加按钮）
- Test: `frontend-vite/src/components/__tests__/TrimDialog.test.tsx`

**Interfaces:**
- Consumes: `speechEndFrame`、既有 `setEndFrame`、`seekToFrame`（第 148 行）、`showSilenceRef`（Task 1）。
- Produces: 按钮文本 `采用参考`；点击后 `endFrame === speechEndFrame`，当前帧读数变为 `帧: 180 / 240`，`确认裁剪` 由禁用变可用。

- [ ] **Step 1: 写失败测试（采用参考落点）**

```tsx
  it('点采用参考:裁剪点吸附到建议帧,确认裁剪变可用', async () => {
    await renderReady()
    // 初始 endFrame == totalFrames → 确认裁剪禁用
    expect(screen.getByText('确认裁剪').closest('button')).toBeDisabled()

    fireEvent.click(screen.getByText('采用参考').closest('button')!)

    // endFrame → 180；当前帧读数更新
    expect(screen.getByText(/帧: 180 \/ 240/)).toBeInTheDocument()
    // 有可裁内容 → 确认裁剪可用
    expect(screen.getByText('确认裁剪').closest('button')).not.toBeDisabled()
  })
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend-vite && ./node_modules/.bin/vitest run src/components/__tests__/TrimDialog.test.tsx -t "采用参考"`
Expected: FAIL —「Unable to find an element with text: 采用参考」。

- [ ] **Step 3: 加 handler**

在 `TrimDialog.tsx` 第 164 行 `handleStep` 之后插入：

```tsx
  const handleApplyReference = () => {
    if (speechEndFrame == null) return
    const clamped = Math.max(minFrames, Math.min(speechEndFrame, totalFrames))
    setEndFrame(clamped)
    seekToFrame(clamped)
  }
```

- [ ] **Step 4: 在参考条内加按钮**

在 Task 1 插入的参考条顶部 `flex items-center gap-3` 那一行的文案 `<div className="flex-1 ...">…</div>` 之后、闭合该 `flex` div 之前，加：

```tsx
                  <button
                    type="button"
                    onClick={handleApplyReference}
                    className="shrink-0 rounded-md bg-amber-500 px-3 py-1.5 text-xs font-semibold text-amber-950 hover:bg-amber-400"
                  >
                    采用参考 →
                  </button>
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd frontend-vite && ./node_modules/.bin/vitest run src/components/__tests__/TrimDialog.test.tsx`
Expected: PASS（含新增 1 项与原有全部）。

- [ ] **Step 6: 提交**

```bash
git add frontend-vite/src/components/TrimDialog.tsx frontend-vite/src/components/__tests__/TrimDialog.test.tsx
git commit -m "feat(trim): 采用参考 snaps trim point to silence reference frame"
```

---

### Task 3: 移除冗余「静音裁剪」按钮与检测逻辑

**Files:**
- Modify: `frontend-vite/src/components/TrimDialog.tsx`（删 `handleDetectSilence`、`isDetectingSilence` state、「静音裁剪」`Button`、`AudioLines` import、`api.detectSilence` 引用）

**Interfaces:**
- Consumes: 无（纯删除；「采用参考」已由 Task 2 取代其功能）。
- Produces: 组件不再引用 `api.detectSilence` / `isDetectingSilence` / `AudioLines`。

- [ ] **Step 1: 确认无其它引用（含 e2e）**

Run:
```bash
cd /home/wayne/tools/video_maker/.claude/worktrees/silence-cut-reference
grep -rnE '静音裁剪|handleDetectSilence|isDetectingSilence' frontend-vite/src frontend-vite/e2e
```
Expected: 命中仅在 `TrimDialog.tsx` 内（若 e2e 有点击「静音裁剪」，改为断言参考条/采用参考；本仓当前无该 e2e）。

- [ ] **Step 2: 删除检测按钮块**

删除 `TrimDialog.tsx` 中「静音裁剪」`<Button>`（约第 419–431 行，`onClick={handleDetectSilence}` 那个整块，含其条件 spinner）。

- [ ] **Step 3: 删除 handler / state / import**

- 删除 `handleDetectSilence` 整个函数（约 239–256 行）。
- 删除 `const [isDetectingSilence, setIsDetectingSilence] = useState(false)`（约第 48 行）。
- 从其余按钮的 `disabled={...}` 表达式中去掉 `|| isDetectingSilence`（第 396、410、438 行等）。
- 从第 2 行 lucide-react import 移除 `AudioLines`（若无其它用途，`grep -n AudioLines TrimDialog.tsx` 确认）。
- 若 `api.detectSilence` 在本组件已无引用，无需额外处理（保留 `api.ts` 定义与后端端点）。

- [ ] **Step 4: 跑测试 + 类型检查确认无回归**

Run:
```bash
cd frontend-vite && ./node_modules/.bin/vitest run src/components/__tests__/TrimDialog.test.tsx && ./node_modules/.bin/tsc --noEmit 2>&1 | grep -i 'TrimDialog' || echo "TrimDialog 无类型错误"
```
Expected: 测试全 PASS；`TrimDialog 无类型错误`（其它文件的既有 tsc 报错与本改动无关）。

- [ ] **Step 5: 提交**

```bash
git add frontend-vite/src/components/TrimDialog.tsx
git commit -m "refactor(trim): remove redundant 静音裁剪 button, superseded by 采用参考"
```

---

## Self-Review

**Spec coverage:**
- 「弹窗内显示帧数剪切参考」→ Task 1 ✅
- 「参考线 vs 手动裁剪点可对照」→ 波形已有（`speechEndFrame` 线 + `endFrame` 手柄），无需改 ✅
- 「采用参考一键吸附」→ Task 2 ✅
- 「无静音时整条隐藏」→ Task 1 Step 1 第二测试 ✅
- 「不碰检测算法/`/trim`/`WaveformTrack`/后端」→ 三个任务均未触及 ✅
- 偏离（复用 `speech_end_frame`、去 +3、删旧按钮）→ 见「计划期决策」，已在 handoff 向用户标注。

**Placeholder scan:** 无 TBD/TODO；每个代码步骤含完整代码与命令。

**Type consistency:** `speechEndFrame`/`silenceFrames`/`showSilenceRef`/`handleApplyReference`/`setEndFrame`/`seekToFrame` 全程一致；断言文本（`尾部静音 60 帧`、`建议剪到第 180 帧`、`可裁静音 60 帧`、`采用参考`、`帧: 180 / 240`）与 JSX 渲染文本逐字对应。
