# 自动静音帧裁剪（尾部）— 设计文档

- **日期**: 2026-06-24
- **分支**: `worktree-feat+auto-silence-trim`
- **状态**: 设计已确认，待写实现计划

## 背景与目标

裁剪对话框（`TrimDialog`）目前支持两种操作：

1. **手动裁剪** — 用户拖滑块选 `endFrame`，点「确认裁剪」应用。
2. **智能校准（SSIM）** — `POST /align-tail-frame`，自动找到与目标尾帧 SSIM 最匹配的帧并**直接裁剪应用**。

本特性新增第三种：**自动检测尾部静音并给出建议裁剪点**。关键差异在于它是 **suggest-only**——只返回建议的帧位，把滑块移过去，由用户预览后再点已有的「确认裁剪」决定是否应用。这正好填补 SSIM 校准「点了就改」与手动「全靠肉眼」之间的空档。

代码库已具备核心能力：`backend/app/agents/video_trimmer.py` 中的 `detect_speech_end()` 用 ffmpeg `silencedetect`（-30dB / 0.3s）检测尾部静音并返回静音起点时间戳。本特性是在其上包一层"建议帧位"并接到前端。

## 范围决策（已确认）

| 决策 | 结论 |
|------|------|
| 裁剪端 | **仅尾部静音**。复用现有单刀尾部裁剪（保留 `1..endFrame`）与 `detect_speech_end`（只检测尾部）。不引入 `start_frame`/区间裁剪。 |
| 触发方式 | **按钮 + 动态检测**。`TrimDialog` 内新增「静音裁剪」按钮；点击时实时检测。**不新增持久化状态字段**。 |
| "没剪过静音帧"判定 | 动态：若检测不到尾部静音（已剪过或本就无静音），返回 `has_silence: false`，前端提示「无尾部静音可裁剪」。 |
| 裁剪落点 | **方案 A**：静音起点 + 小段缓冲（`SILENCE_TAIL_PADDING_FRAMES = 3` 帧），保留说话结束后的呼吸感，避免切得过死。缓冲为后端常量，可调。 |
| 应用方式 | suggest-only。新端点不写文件；真正裁剪由用户随后点「确认裁剪」走现有 `trim` 端点完成。 |

## 架构

三处改动 + 测试，全部复用现有模式：

```
[静音裁剪按钮] --点击--> POST /detect-silence (只读, 不写文件)
                              |
                         detect_speech_end() -> 静音起点时间戳
                              |
                         suggest_silence_trim() -> 建议 endFrame
                              |
                         返回 {has_silence, suggested_end_frame, ...}
                              |
        has_silence: true  -> setEndFrame(suggested)  滑块跳转, 不关闭对话框
        has_silence: false -> toast「无尾部静音可裁剪」
                              |
                     用户预览/微调 -> 点「确认裁剪」-> 现有 trim 端点 (真正应用+状态重置)
```

### 1. 后端 — service 层

`backend/app/agents/video_trimmer.py` 新增纯检测函数（**不写任何文件**）：

```python
SILENCE_TAIL_PADDING_FRAMES = 3   # 落点 A：静音起点后保留的缓冲帧

def suggest_silence_trim(
    video_path: str,
    padding_frames: int = SILENCE_TAIL_PADDING_FRAMES,
) -> dict | None:
    """检测尾部静音，返回建议保留帧数。不修改任何文件。

    返回 {
        "suggested_end_frame": int,
        "silence_start_time": float,
        "fps": float,
        "total_frames": int,
        "duration": float,
    }
    无尾部静音 / 无可裁时返回 None。
    """
```

逻辑：
1. `speech_end = detect_speech_end(video_path)`（复用现有，-30dB / 0.3s）。`None` → 返回 `None`。
2. `info = get_video_info(video_path)` 拿 `fps` / `total_frames` / `duration`。
3. `suggested = round(speech_end * fps) + padding_frames`。
4. 钳制：
   - `suggested < 24` → 钳到 `24`（与现有 trim 的最小帧下限一致）。
   - `suggested >= total_frames` → 返回 `None`（缓冲后已到结尾，无可裁）。
5. 返回 dict。

### 2. 后端 — API 端点

`backend/app/api/pipeline.py` 新增，**镜像 `align-tail-frame` 的签名但为只读**：

```
POST /projects/{project_id}/shots/{shot_id}/detect-silence
请求体：无
响应：{
  "has_silence": bool,
  "suggested_end_frame": int | null,
  "silence_start_time": float | null,
  "fps": float,
  "total_frames": int,
  "duration": float
}
```

- `suggest_silence_trim` 返回 `None` → `has_silence: false`，`suggested_end_frame` / `silence_start_time` 为 `null`，其余字段仍从 `get_video_info()` 填充（前端可能需要刷新元数据）。
- 返回 `dict` → `has_silence: true`，回填各字段。

### 3. 前端 — TrimDialog

**`frontend-vite/src/lib/api.ts`** 新增：

```typescript
detectSilence: (projectId, shotId) => Promise<{
  has_silence: boolean
  suggested_end_frame: number | null
  silence_start_time: number | null
  fps: number
  total_frames: number
  duration: number
}>
```

**`frontend-vite/src/components/TrimDialog.tsx`** 在「智能校准」按钮旁新增「静音裁剪」按钮：

- 点击 → `api.detectSilence(projectId, shot.shot_id)`：
  - `has_silence: true` → `setEndFrame(suggested_end_frame)`，滑块与红蓝（保留/丢弃）指示自动跳到建议帧，**不关闭对话框**。用户可预览、用 ±1/±10 帧微调，再点已有的「确认裁剪」。
  - `has_silence: false` → toast「无尾部静音可裁剪」。
- loading 态复用现有按钮 disabled 模式。

**与「智能校准」的关键区别**：智能校准点了直接 apply 并关闭对话框；静音裁剪**只移动滑块**，应用与否完全交给用户后续的「确认裁剪」——这就是"预览再确定"。

## 素材文件审计（遵循 CLAUDE.md）

新端点 `detect-silence` **零下游副作用**：

- [x] 不写 / 重命名 / 删除任何素材文件（`output.mp4`、`output_original.mp4`、`last_frame.png` 等均不动）。
- [x] 不创建 / 删除备份文件。
- [x] 不重置 `cc_status` / `vc_status`。
- [x] 不重新提取 `last_frame`。

真正的文件变更与状态重置仍由用户随后点「确认裁剪」触发的**现有** `trim` 端点完成，其审计逻辑已存在、未改动。因此本特性不引入新的素材一致性风险。

## 边界处理

| 情况 | 行为 |
|------|------|
| 无尾部静音 / `detect_speech_end` 返回 `None` | `has_silence: false`，前端 toast「无尾部静音可裁剪」。天然覆盖"已剪过静音"。 |
| 建议帧 `< 24` | 钳到 24 并正常返回 `has_silence: true`（让用户看到下限）。 |
| 建议帧 `>= total_frames`（缓冲后到结尾） | 视为无可裁，`has_silence: false`。 |
| 视频无音轨 | `detect_speech_end` 的 ffmpeg 调用按现有行为处理（无静音段→`None`）→ `has_silence: false`。 |

## 测试

遵循项目规则：**所有 AI/模型调用必须 mock；不跑真 ffmpeg；AI 触发型端点在 Playwright 中 mock。**

### 后端单测（`backend/tests/`）

针对 `suggest_silence_trim`，mock `detect_speech_end` 与 `get_video_info`（不跑真 ffmpeg）：

1. **有尾部静音** → 返回 `suggested = round(speech_end*fps)+3`，字段正确。
2. **全程有声**（`detect_speech_end` 返回 `None`）→ 函数返回 `None`。
3. **静音过短**（建议帧 < 24）→ 钳到 24。
4. **静音占满 / 缓冲后到结尾**（建议帧 ≥ total_frames）→ 返回 `None`。

可选：端点测试，验证 `None` → `has_silence: false` 且其余字段从 `get_video_info` 回填。

### Playwright

- mock `**/api/projects/*/shots/*/detect-silence`（避免触发真 ffmpeg/重活）。
- 验证：点「静音裁剪」后滑块跳到 mock 返回的帧；`has_silence: false` 时出现 toast。

## 改动文件清单

| 文件 | 改动 |
|------|------|
| `backend/app/agents/video_trimmer.py` | 新增 `SILENCE_TAIL_PADDING_FRAMES` 常量 + `suggest_silence_trim()` |
| `backend/app/api/pipeline.py` | 新增 `POST /detect-silence` 端点 |
| `frontend-vite/src/lib/api.ts` | 新增 `detectSilence()` |
| `frontend-vite/src/components/TrimDialog.tsx` | 新增「静音裁剪」按钮 + 处理逻辑 |
| `backend/tests/...` | `suggest_silence_trim` 单测 |
| Playwright 测试 | `detect-silence` mock + UI 断言 |

## 非目标（YAGNI）

- 不做头部 / 区间静音裁剪。
- 不引入持久化的 `silence_trimmed` 状态字段。
- 不在 `detect-silence` 端点里直接应用裁剪（保持 suggest-only）。
- 不暴露静音阈值（-30dB / 0.3s）/ 缓冲帧数为用户可调参数（后端常量即可）。
