# 音色校准（Voice Calibration）— 设计文档

**日期**: 2026-06-22
**状态**: 待实现
**作者**: brainstorming session

## 1. 背景与目标

项目已有 **语音克隆（VC）** 功能：用 CosyVoice VC2 把每个分镜的音频转换成某个"基准音色"。当前基准音色只能来自一个**已完成的分镜**（在分镜卡片上点「设为基准」，写入 `Project.reference_voice_shot_id`），并且转换是**手动触发**的（单镜 / 校准全部）。

本次需求在已有 VC 引擎之上扩展两点：

1. **基准音色可由上传文件提供** —— 允许上传 `mp4 / m4a / wav` 作为基准音色，而不只是选一个已有分镜。
2. **自动校准流水线** —— 一个项目级开关，打开后，新生成的分镜在视频流水线完成后**自动接一个音色校准**，无需手动点击。

### 非目标（YAGNI）

- 不替换、不重写已有的 CosyVoice 转换引擎、`output_pre_vc.mp4` 备份策略、`vc_status` 状态机、revert 逻辑 —— 全部复用。
- 不做基准音色的多文件管理 / 历史版本。
- 不对已完成分镜做"自动回填"校准（见 §4 retroactive = (a)）。
- 不引入新的任务队列；复用现有 `arq:vc` 队列。

## 2. 方案选择

**采用方案 A：扩展已有 reference-voice 系统**（而非新建一套并行的"音色校准"系统）。

理由：实际的音色转换由现有 VC 机器完成，本需求只改变 **基准音色的来源**（上传文件）和 **触发时机**（自动）。扩展现有系统可复用转换引擎，避免两套重叠系统。

## 3. 数据模型

`Project` 模型新增两个字段（`backend/app/models/project.py`）：

| 字段 | 类型 | 含义 |
|------|------|------|
| `reference_voice_path` | `str \| None` | 上传基准音色归一化后的 `prompt.wav` 路径（文件来源） |
| `auto_voice_calibrate` | `bool`（默认 `False`） | 项目级自动校准开关 |

已有的 `reference_voice_shot_id` 保留。

### 互斥性（Mutual Exclusivity）

一个项目同一时刻只需要**一个**基准音色，来源**二选一**：分镜 **或** 上传文件，绝不同时存在。

- 在 API 层强制：设置其中一个来源时清空另一个。
- 任意时刻 `reference_voice_shot_id` 与 `reference_voice_path` 至多一个非空。

### 单一解析器（Single Resolver）

新增一个统一函数，作为"本项目 VC 该用哪个 `prompt_wav`"的唯一来源：

```python
def resolve_reference_prompt_wav(project) -> Path | None:
    if project.reference_voice_path:        # 上传文件来源（互斥，只会有一个被设置）
        return Path(project.reference_voice_path)
    if project.reference_voice_shot_id:     # 已有的分镜来源
        return get_original_video_for_audio(reference_shot)  # → 提取音频
    return None
```

现有 `run_voice_convert` 任务重构为：`prompt_wav` 从该解析器获取，而非硬编码"从基准分镜提取"。**这是唯一的集成点** —— 下游（CosyVoice 调用、`output_pre_vc.mp4` 备份、`vc_status`、revert）全部不变。

### 上传文件处理

上传时用 ffmpeg 把 `mp4 / m4a / wav` 归一化为 **单声道 `prompt.wav`（16kHz，CosyVoice 期望采样率）**，存放于 `projects/{id}/reference_voice/prompt.wav`：

- `mp4` → 提取音频流；
- `m4a / wav` → 转码 / 重采样。

归一化后丢弃原始上传文件（只需要 wav）。

## 4. API 端点

位于 `backend/app/api/pipeline.py`：

| 端点 | 用途 |
|------|------|
| `POST /projects/{id}/reference-voice/upload` | Multipart 上传 mp4/m4a/wav → 归一化为 `prompt.wav`，设置 `reference_voice_path`，**清空 `reference_voice_shot_id`**。返回更新后的 project。 |
| `DELETE /projects/{id}/reference-voice` | 扩展：清空**当前生效的**来源（分镜 id 或上传文件）；若 `auto_voice_calibrate` 为开，则一并关闭（无基准音色不能自动运行）。 |
| `POST /projects/{id}/auto-voice-calibrate` | Body `{enabled: bool}`。置 `true` 要求已存在基准音色（否则返回 409）。 |
| `POST /projects/{id}/reference-voice`（已有，分镜来源） | 扩展：标记某分镜为基准时，清空 `reference_voice_path`。 |

### 上传校验

- 扩展名白名单：`.mp4 / .m4a / .wav`；
- 大小上限：**50 MB**；
- ffprobe 确认存在音频流（拒绝无声 / 无音频流的 mp4）。

### Retroactive 行为 = (a)

`auto-voice-calibrate` 置 `true` **不会**对已完成分镜回填校准；仅对**之后生成**的分镜生效。已完成分镜仍可用「校准全部」手动处理。

## 5. 自动触发接线（核心）

在 worker 中，`run_shot_pipeline` 把分镜标记为 `COMPLETED` 的位置（`backend/worker/tasks.py` ~line 500）追加一个收尾钩子：

```python
if project.auto_voice_calibrate and resolve_reference_prompt_wav(project) is not None:
    if shot.id != project.reference_voice_shot_id and shot.vc_status is None:
        enqueue("run_voice_convert", shot_id, _queue_name="arq:vc")
```

关键性质：

- **`vc_status is None` 守卫**：防止重复入队（`vc_status` 在 `converting`/`done` 时不再触发）。
- **只对之后完成的分镜触发**（retroactive = a）；已完成分镜不受影响。
- 复用现有 `arq:vc` 队列 → 校准并行执行，不阻塞视频生成。
- **文件来源**时无分镜被排除（`reference_voice_shot_id` 为空，该判断恒真）。**分镜来源**时跳过基准分镜本身。
- 重新生成 / 裁剪分镜视频会把 `vc_status` 重置为 `None`（既有的素材审计规则）→ 自动钩子会重新校准。

### 关于 `vc_status`

`vc_status` 是分镜级字段（`null | "converting" | "done" | "failed"`），用途：

1. **UI 状态**：`ShotCard` 渲染 converting 转圈 / done 徽标 / failed 错误；`ProgressStream` 在任意分镜 `converting` 时保持进度条活动。
2. **门控转换按钮**：手动「转换」按钮仅在 `!shot.vc_status`（null）时显示 —— 即"可校准"。
3. **视频变更时重置**：regenerate / trim / revert 时置 `null`（素材审计规则：新视频 ⇒ 旧转换过期）。
4. **revert 守卫**：voice-revert 端点要求 `vc_status == "done"` 才恢复 `output_pre_vc.mp4`。

## 6. 前端 UI

当前「设为基准」控件在**每个分镜卡片**上（per-shot）。而上传文件、自动开关、「校准全部」都是**项目级**概念。因此在 `ShotsPage` 新增一个紧凑的**项目级音色校准面板**（靠近现有批量控件），分镜卡片基本保持不变。

### 新增「音色校准」面板（项目级，可折叠头部栏）

```
┌─ 音色校准 ───────────────────────────────────────────────┐
│  基准音色:  ● 上传文件: prompt.wav  [更换] [移除]          │
│             ( 或 在某个分镜上点「设为基准」 )               │
│                                                          │
│  [ ⬆ 上传基准音色 (mp4/m4a/wav) ]                         │
│                                                          │
│  ☑ 自动音色校准   （新生成的分镜自动校准）                  │
│  [ 校准全部 ]                                             │
└──────────────────────────────────────────────────────────┘
```

行为：

- **基准音色显示**：展示当前生效来源 —— `上传文件: <name>` 或 `分镜 N`（标记了分镜时），体现互斥。`[移除]` 清空。
- **上传按钮** → 文件选择器（`accept=".mp4,.m4a,.wav"`）→ `POST .../reference-voice/upload`。成功后显示翻转为文件来源；若原先标记了某分镜，其「基准音色」徽标自动清除（服务端已清空 shot id，前端重新拉取）。
- **自动音色校准开关** —— 无基准音色时禁用 / 置灰，tooltip "先设置基准音色"。切换调用 `POST .../auto-voice-calibrate`。按选项 (a)，打开不回填，仅影响之后生成的分镜 —— 在开关 helper text 注明"仅对之后生成的分镜生效"。
- **校准全部** —— 既有批量按钮，移入此面板以便发现（同 `voice-convert-all` 端点）。这是把音色应用到已完成分镜的方式。

### 分镜卡片（`ShotCard`）

基本不变 —— 保留「设为基准 / 取消基准」开关（仍是选分镜作基准的有效方式）与既有 `vc_status` 转圈 / done / failed 显示。唯一新增：当 `auto_voice_calibrate` 为开时，在 converting 转圈旁加一个小「自动」提示，区分自动 vs 手动触发。

## 7. 数据库迁移

`backend/app/db.py` 风格的轻量列添加（参照现有 `vc_status` 的 `ALTER TABLE ADD COLUMN` 模式）：

- `projects.reference_voice_path VARCHAR`（nullable）
- `projects.auto_voice_calibrate BOOLEAN DEFAULT 0`

## 8. 受影响文件清单

| 文件 | 改动 |
|------|------|
| `backend/app/models/project.py` | 新增 `reference_voice_path`、`auto_voice_calibrate` 字段 |
| `backend/app/db.py` | 两个新列的 ADD COLUMN 迁移 |
| `backend/app/services/storage.py` | 新增 `reference_voice` 目录 / `prompt.wav` 路径辅助函数 |
| `backend/app/services/`（新增或现有 voice 服务） | `resolve_reference_prompt_wav()` 解析器 + ffmpeg 归一化辅助 |
| `backend/app/api/pipeline.py` | 上传端点、auto 开关端点、扩展现有 reference-voice 端点（互斥清空） |
| `backend/worker/tasks.py` | `run_voice_convert` 改用解析器取 `prompt_wav`；`run_shot_pipeline` 完成处加自动触发钩子 |
| `backend/app/models/schemas.py` / project 序列化 | 暴露 `reference_voice_path`、`auto_voice_calibrate` 给前端 |
| `frontend-vite/src/lib/types.ts` / `api.ts` | 新字段类型 + 新端点客户端方法 |
| `frontend-vite/src/pages/ShotsPage.tsx` | 新增项目级音色校准面板 + handlers |
| `frontend-vite/src/components/ShotCard.tsx` | 自动校准时的「自动」提示 |

## 9. 测试要点

> 遵守项目规则：测试中 mock 所有 LLM / 模型调用（CosyVoice 推理）以避免计费；其余用真实服务。

- **上传归一化**：mp4 / m4a / wav 三种输入均产出 16kHz 单声道 `prompt.wav`；无音频流的 mp4 被拒（409/400）。
- **互斥性**：上传文件清空 `reference_voice_shot_id`；标记分镜清空 `reference_voice_path`；二者不同时非空。
- **解析器**：文件来源返回上传 wav；分镜来源走 `get_original_video_for_audio`；都没有时返回 `None`。
- **auto 开关门控**：无基准音色时置 `true` 返回 409；移除基准音色时自动关闭。
- **自动触发钩子**：开关打开后新完成的分镜入队 VC（mock CosyVoice）；`vc_status` 非 null 时不重复入队；分镜来源时基准分镜本身被跳过；retroactive=(a) 不触碰已完成分镜。
- **前端**：面板在有/无基准音色时的禁用态；上传成功后分镜徽标清除；自动开关 helper text。
