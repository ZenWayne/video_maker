# 静音帧剪切参考（Silence Cut Reference）设计稿

日期：2026-07-02
分支：`worktree-silence-cut-reference`
状态：待评审

## 一句话

在**裁剪弹窗**（`TrimDialog`）里，把「尾部静音」检测结果以**帧数**形式明确呈现为一个**剪切参考**：告诉用户「尾部静音 N 帧、建议剪到第 X 帧」，并可一键把裁剪点吸附到该参考帧。

## 背景 / 动机

镜头视频尾部常有一段角色说完话后的静音，用户希望据此决定裁剪点。当前系统已能**检测**尾部静音，但信息以「波形标记 + 跳点」的形式存在，用户看不到一个明确的**帧数参考**（「到底该剪多少帧 / 剪到第几帧」）。本功能把这个帧数参考显性化，作为剪切时的量化依据。

## 现状（已存在，复用）

- **后端** `app/agents/video_trimmer.py`
  - `detect_speech_end(video_path)` → 尾部静音起点（秒）。
  - `speech_end_info(video_path, fps)` → `(秒, 帧号)`。
  - `SILENCE_TAIL_PADDING_FRAMES = 3`（说完话后保留 3 帧呼吸感）。
- **后端端点** `POST /api/projects/{id}/shots/{shot_id}/detect-silence`
  - 返回 `{ has_silence, suggested_end_frame, silence_start_time, fps, total_frames, duration }`。
  - `suggested_end_frame` = 静音起点帧 + 呼吸帧（已含 padding）。
- **剪切元数据加载**（弹窗打开时）已返回 `speech_end_frame` / `speech_end_sec`，`TrimDialog` 存为 `speechEndFrame`。
- **前端** `TrimDialog.tsx` / `WaveformTrack.tsx`
  - `WaveformTrack` 已能画 `speechEndFrame` 参考线（黄）与 `endFrame` 手动裁剪点（绿）。
  - `handleDetectSilence()`「静音裁剪」按钮：检测后把 `endFrame` 跳到 `suggested_end_frame` 并 seek。

**结论：检测与裁剪链路都已具备，本功能不新增检测/裁剪逻辑，也不需要后端改动。** 只在剪切弹窗里把「帧数参考」显性呈现，并让「采用参考」与手动裁剪点的关系更清晰。

## 目标 / 非目标

**目标**
1. 在 `TrimDialog` 里显示一个**静音剪切参考读数**（数字）：总帧数、建议剪切点（帧）、可裁掉的尾部静音（帧）。
2. 让波形上的**参考线（静音起点）**与**手动裁剪点**始终可对照（参考线在有静音时可见）。
3. 提供**「采用参考」**动作：把手动裁剪点吸附到建议帧（等价于现有静音裁剪按钮的效果，但参考读数保留可见，不是一次性跳走）。

**非目标（YAGNI）**
- 不新增自动追加静音帧的功能。
- 不在 `ShotCard` 卡片上显示（已与用户确认：只放剪切弹窗）。
- 不改静音检测阈值 / 算法（沿用 -30dB / 0.3s / +3 帧）。
- 不做整段中部静音分析，只针对**尾部**静音（与现有 `detect_speech_end` 一致）。

## 设计

### 数据来源（无需后端改动）

参考帧数全部可从已有数据推导：

- `referenceFrame`（建议剪切点）= `detect-silence.suggested_end_frame`（含呼吸帧）。
  弹窗打开时也已有 `speechEndFrame`（= `speech_end_frame`），二者语义一致，取已加载者即可，避免多一次请求。
- `silenceFrames`（可裁掉的尾部静音）= `totalFrames - referenceFrame`。
- `speechEndSec` / `duration` 已有，用于把帧数换算成「秒」辅助文案。

> 若评审希望端点显式回帧数，可选地在 `detect-silence` 响应加 `silence_frames`（= `total_frames - suggested_end_frame`）与 `speech_end_frame`（= `round(silence_start_time*fps)`）两个便捷字段。默认**不加**——前端可直接推导，保持最小改动。

### UI（在 `TrimDialog` 内）

在视频预览与波形之间，新增一个**静音剪切参考条**（仅当 `has_silence && referenceFrame != null` 时显示）：

```
🔇  检测到尾部静音 17 帧 (0.71s) · 建议从第 168 帧剪切        [ 采用参考 → ]
```

波形下方新增三格**帧数读数**（复用现有值，无新状态）：

```
总帧数 185   |   建议剪切点 168 帧   |   可裁尾部静音 17 帧
```

波形轨（`WaveformTrack`，已支持，无需改）：
- 黄色竖线 = 静音起点/参考帧（`speechEndFrame`）。
- 绿色手柄 = 手动裁剪点（`endFrame`）。
- 两者并存，用户能直观看到「参考 vs 当前选择」。

### 交互 / 行为

- 弹窗打开：若元数据带来 `speechEndFrame`，参考条与读数即显示（`endFrame` 仍为用户上次值 / 全长，不自动跳）。
- 点「采用参考」或现有「静音裁剪」按钮：`setEndFrame(referenceFrame)` + seek；参考条与读数**保留可见**（不清空），用户可反复对照/微调。
- 点「确认裁剪」：沿用现有 `handleTrim()` → `POST /trim`（帧精确裁剪）。本功能不碰裁剪执行。
- 无尾部静音（`has_silence=false` 或 `silenceFrames ≤ 0`）：**参考条与三格读数整条隐藏**，弹窗保持与当前一致（不新增任何占位文案）。

### 组件边界

| 单元 | 职责 | 依赖 | 是否改动 |
|---|---|---|---|
| `detect-silence` 端点 | 返回静音检测结果（帧/秒） | video_trimmer | 不改（除非选择加便捷字段） |
| `TrimDialog` | 组织参考条 + 读数 + 采用参考按钮；持有 `speechEndFrame` / `endFrame` / `totalFrames` | api.detectSilence, WaveformTrack | **改**（新增展示，无新数据流） |
| `WaveformTrack` | 画参考线 + 手动点 + 播放头 | waveform utils | 不改 |
| `api.detectSilence` | 客户端调用 | — | 不改（除非加字段） |

## 测试

遵循项目规则：真实后端/DB/端点，只在计费边界（模型生成）打桩；这里不涉及模型调用。

- **后端（若加便捷字段才需要）**：单测 `detect-silence` 对一段「尾部含静音」的真实小视频返回 `suggested_end_frame < total_frames` 且 `silence_frames = total_frames - suggested_end_frame`；对「无尾部静音」视频返回 `has_silence=false`。（用已有的、由过往生成产出的真实 mp4，勿调模型。）
- **前端（e2e/组件，不 mock 数据流）**：
  - 打开裁剪弹窗 → 参考条显示正确帧数（断言真实 `detect-silence` / 元数据返回值渲染出来，而非注入的假值）。
  - 点「采用参考」→ `endFrame` 变为参考帧、波形手柄移动到参考线位置、参考读数仍在。
  - 无静音视频 → 不显示参考条。
- 复用现有 `TrimDialog.test.tsx` fixture 结构，扩展断言；波形渲染断言用现有 `WaveformTrack` 测试模式。

## 风险 / 边界情况

- **整段静音**：`detect_speech_end` 已在起点 <0.5s 时返回 None（视为无有效尾部静音），参考条不显示。
- **参考帧 ≥ 总帧**（静音极短或 padding 顶到末尾）：`silenceFrames ≤ 0` → 视为无可裁静音，不显示参考条。
- **fps/帧数来自 ffprobe**：与现有裁剪同源，不引入新的一致性风险。

## 落地范围小结

- 主要改动：`TrimDialog.tsx`（新增参考条 + 三格读数 + 「采用参考」按钮文案/行为），纯展示层。
- 可选：`detect-silence` 端点加 `silence_frames` / `speech_end_frame` 便捷字段（默认不做）。
- 不改：检测算法、`/trim` 执行、`WaveformTrack`。
