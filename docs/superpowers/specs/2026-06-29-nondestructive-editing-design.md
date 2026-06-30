# 非破坏式编辑（裁剪 + 配音）设计

- 日期：2026-06-29
- 范围：把镜头的 **裁剪（trim）** 和 **配音（VC）** 从「物理改写视频文件」改成「只改元数据 + 播放/导出时合成」
- 状态：设计已与用户逐段确认，待评审后进入 writing-plans

## 1. 背景与目标

### 现状（问题）
- 每个镜头有可变的 `output.mp4`（`shot.video_path`）。裁剪会物理重写出 `trimmed_<ts>_<uuid>.mp4`，VC 会 remux 出 `vc_<ts>_<uuid>.mp4`，并维护一系列备份/状态联动（CLAUDE.md 的「Shot 素材文件变更审计」规则正是为此而生）。
- 每次编辑都跑 ffmpeg 重编码 → 慢、占存储、有「读到过期素材」隐患。
- 撤销依赖文件还原，不是常数级操作。

### 目标（用户确认的三个驱动）
1. **秒级撤销/重做** —— 编辑只改元数据，撤销 = 清元数据。
2. **省存储/省算力** —— 不再为每次编辑落多份 mp4，不再每次重编码（无损）。
3. **实时预览** —— 前端即时合成预览裁剪/换音轨效果。

### 范围决策（用户确认）
- 仅 **裁剪 + 配音（VC）** 改为非破坏式。
- **CC（角色校准）维持现状**：它改的是关键帧静图 `last_frame`，不在播放画面里。
- **不保留向后兼容**：全量重构，假设现有数据可丢弃/重生，不写迁移。
- 裁剪只支持「从头保留 N 帧」单值 `trim_frames`，不做区间裁剪。

### 核心架构洞察
编辑元数据（EDL = Edit Decision List）有**两个消费方**：**预览（前端）** 与 **导出（后端）**。两者读同一份 DB 元数据，保证「预览所见 = 成片所得」。这是贯穿全设计的不变量。

## 2. 数据模型 & 文件布局

### 单一真相源
镜头生成时只写一份**不可变**的源视频，从此裁剪/VC 永不改动它。复用本分支已有的 prefix+uuid 约定（commit `e77f35a`）：

| 文件 | 说明 |
|------|------|
| `output_<ts>_<uuid>.mp4` | **不可变源**（原画 + 原音），`video_path` 指它。生成时写一次 |
| `last_frame_<ts>_<uuid>.png` | 未校准末帧，喂给**下一镜头**的关键帧；裁剪时重抽**新**文件 |
| `cc_<ts>_<uuid>.png` | CC 校准后的末帧，单独写、**绝不覆盖** `last_frame_` |
| `audio_vc_<ts>_<uuid>.wav` | VC 替换音轨；`vc_audio_path` 指它 |
| `first_frame.png` / `target_last_frame.png` | 维持现状（裁剪不影响首帧；CC/尾帧不变） |

**废弃**：`trimmed_*.mp4`、`vc_*.mp4`、`shot_pre_vc_video_path`、持久化的 `audio_original.wav`（VC 输入改为从源临时抽取，不落盘）。

> 命名原则（用户强调）：**绝不原地覆盖**。每个产物用 `ts_uuid_name()` 生成唯一名，DB 指针指向当前版本；被取代的旧文件显式删除以防累积。

### Shot DB 字段变更（`backend/app/models/project.py`）
- `video_path` → 指向 `output_<ts>_<uuid>.mp4`（语义 = 播放器加载的视频文件；保留字段名以减小改动面）
- 新增 `trim_frames` (Int, nullable) —— 从头保留帧数；`null` = 不裁剪
- 新增 `source_fps` (Float)、`source_frames` (Int) —— 生成时写入；供前端帧↔秒换算、裁剪滑块上界
- 新增 `vc_audio_path` (Str, nullable) —— 指向 `audio_vc_<ts>_<uuid>.wav`；`null` = 用源原音
- `vc_status` / `cc_status` / `tf_status` —— 保留（异步任务状态）

### Shot 序列化
新增「有效播放描述」给前端：`{ video_url, trim_end_sec, audio_url|null }`，全部由上面字段派生（`trim_end_sec = trim_frames / source_fps`）。

### storage.py 改动（`backend/app/services/storage.py`）
- 新增 `shot_source_path()`（= 当前 `pristine_video_path` 的语义，定位 `output_` 源）
- 删除 `shot_pre_vc_video_path()`
- `get_original_video_for_audio()` 简化为返回 `output_` 源
- `shot_audio_vc_path()` 改用 `ts_uuid_name()`
- `pristine_last_frame_path()` 保留（CC 重置目标）

## 3. 编辑流程（后端）

所有编辑 = 改元数据 +（必要时）抽一帧，**不再 remux / 不再落备份 mp4**。

### 生成时（worker）
写 `output_<ts>_<uuid>.mp4`，记录 `source_fps`、`source_frames`；`trim_frames=null`、`vc_audio_path=null`。

### 裁剪 `POST /trim {frames:N}`（改写 `pipeline.py:1125`）
1. `shot.trim_frames = N`（纯 DB）
2. 从 `output_` 源第 `N-1` 帧抽**新** `last_frame_<ts>_<uuid>.png`（单帧 ffmpeg，廉价，无重编码），更新 `last_frame_path`，删掉被取代的旧 `last_frame_*` / `cc_*`
3. 因末帧图变了 → **重置 CC**（`cc_status=null`）
4. **不碰 VC**（见下方关键决策）

### 还原裁剪 `POST /restore-trim`
`trim_frames=null` → 从源末帧抽新 `last_frame_` → 重置 CC。瞬时。

### 配音 `POST /voice-convert`（改写 `worker/tasks.py:822`）
1. 从 `output_` 源抽**整条**音轨 → 临时 `audio_in.wav`（不落盘）
2. CosyVoice 转换 → `audio_vc_<ts>_<uuid>.wav`（落盘）
3. `vc_audio_path = 该 wav`、`vc_status="done"`
4. **源 mp4 全程不动，无备份、无 remux**

### 撤销配音 `POST /voice-revert`
清 `vc_audio_path` + `vc_status`，删该 wav。瞬时。

### 关键决策：裁剪 **不** 作废 VC
- 旧模型里裁剪物理重写短 `output.mp4`，烤进去的音轨随之失效 → 才需作废 VC。
- 新模型里 `audio_vc.wav` 是**全长独立文件**（VC 转换的是整条源音轨，不按 trim 截）。裁剪只是改播放/导出停在第 N 帧，播放与导出时把视频与 vc 音轨**一起钳到 T**。vc 音轨本身永远有效 → **裁剪与 VC 正交，互不作废**。
- 对比：CC 必须随裁剪重置，因为末帧**图**真的换了；VC 不需要，因为音轨内容没变。

## 4. 前端播放器（预览合成）

新增可复用组件 `<ShotPlayer shot=…>`（`frontend-vite/src/components/`），按「有效播放描述」选三种模式：

- **模式 1 原片**（无裁剪、无 VC）：`<video src={video_url} controls>`，同现状。
- **模式 2 仅裁剪**：`<video>` + `trim_end_sec`；`onTimeUpdate` 中 `if (v.currentTime >= trim_end_sec) v.pause()`（或循环回 0）；进度条 max = `trim_end_sec`。纯钳制，无第二元素。
- **模式 3 裁剪 + VC（双元素同步）**：
  - DOM：`<video muted>`（出画面）+ 隐藏 `<audio src={audio_url}>`（出声）。
  - `useShotSync` hook：
    - `play/pause`：两者联动，以 video 为主钟。
    - `seek`：set `video.currentTime` → `onSeeked` 里 `audio.currentTime = video.currentTime`。
    - **漂移纠正**：`onTimeUpdate`（~4Hz）中 `if (|audio.currentTime - video.currentTime| > 0.15) audio.currentTime = video.currentTime`。
    - `trim_end_sec` 到点两者一起 pause/loop。
    - 缓冲：`waiting` → 都 pause，`canplay` → 对齐后恢复。
  - **A/B 音轨开关**（用户要求）：小开关切换声音源（vc 音轨 ⇄ 源原音），默认 vc 音轨。
  - 容错：`audio_url` 加载失败 → 退回模式 2（放源原音）+ toast「配音音轨加载失败」。

## 5. 导出（后端从 EDL 现烤）

导出是 EDL 的第二个消费方，是 ffmpeg 跑 trim/remux 的**唯一**地方（从「每次编辑」挪到「导出一次」= 省算力的兑现）。

### 改动入口：`worker/tasks.py:733 run_merger`
新增一层 `effective_clip_paths(shots)`：
- **未编辑镜头**（`trim_frames=null` 且 `vc_audio_path=null`）：直接用 `output_` 源，**零开销透传**。
- **已编辑镜头**：现烤临时 effective clip（trim + 音轨替换在**同一次** ffmpeg）：
  ```
  ffmpeg -i <output_源.mp4> [-i <audio_vc.wav>] \
         -map 0:v [-map 1:a | -map 0:a] \
         -t <T = trim_frames / source_fps>   # 帧精度沿用 video_trimmer 的 -vframes 逻辑
         -c:v libx264 … -c:a aac …  <temp_clip.mp4>
  ```
- 把这批路径（源透传 + 临时烤）喂给**现有** `merge_shots_with_crossfade` / `merge_shots`，merger.py 几乎不动。
- `finally` 清理临时 clip。

### 边界处理
- 单镜头项目：未编辑继续 stream-copy 源；已编辑则必须烤。
- 已编辑镜头必然 re-encode（帧精度），一次性成本可接受。
- 交叉转场 xfade 偏移按 effective clip 实际时长算（已钳到 T，自然正确）。

## 6. 错误处理
- 前端 VC 音轨加载失败 → 退回模式 2（源原音）+ toast。
- `trim_frames > source_frames` → 后端钳到 `source_frames`（或 400）。
- 导出时 `vc_audio_path` 文件丢失 → 警告日志 + 退回源原音，不阻塞导出。
- 某镜头 effective clip 烤制失败 → 导出整体失败并报出是哪个 shot。
- `output_` 源缺失（生成未完成）→ 播放器占位，导出明确报错。

## 7. 测试

> md5 的 lossy 陷阱：effective clip / 最终合成若用 libx264 重编码，末帧是源帧的**有损**版本，严格 md5 会 flaky。对策：**测试专用无损 fixture**——重编码环节改 `-c:v ffv1`、合并用 concat `-c copy`，使整条链字节保真；生产仍用 libx264。

### 端到端（核心）`test_final_video_shot_last_frames`
1. fixture 跑完整导出，**crossfade 关闭**（交叉转场会混合边界帧，那帧不再等于任何单镜头末帧）。
2. 按各镜头 effective 时长累加，算出每个分镜末帧在 `final/merged.mp4` 的时间戳。
3. ffmpeg 从最终视频抽这些帧 `F_i`，从 `output_` 源直抽第 `N_i-1` 帧 `L_i`。
4. 逐分镜断言 `md5(F_i) == md5(L_i)`（无损 fixture 下严格成立）。
   - 锁住的不变量链：`源帧 N-1 == 分镜 last_frame == 最终视频里的那一帧`。

### 单元（字节无损，天然严格 md5）
- `test_trim_last_frame_md5`：trim 抽出的 `last_frame_<uuid>.png` == 源帧 N-1。
- `test_source_immutable_md5`：`/trim`·`/voice-convert`·`/voice-revert` 前后 `output_<uuid>.mp4` md5 不变（证明真·不改视频）。
- `test_effective_clip_paths`：未编辑镜头恒等透传源路径；已编辑返回临时烤片。
- `test_preview_export_parity`：前端钳制时长 `trim_frames/fps` == 导出 clip 时长。

### 前端
- `ShotPlayer` 三模式选择 + A/B 开关；`useShotSync` 同步/漂移纠正（vitest）。
- Playwright 端到端：按 CLAUDE.md **mock 所有 AI 端点**、用真实 redis。

### 通用约束（项目记忆）
- 所有 CC/VC 模型调用（CosyVoice、Gemini 等）**必须 mock**，不烧钱。
- 测试用 `uv run pytest`；不硬编码绝对路径。

## 8. 受影响文件清单（实现参考）
- `backend/app/models/project.py` —— Shot 新字段
- `backend/app/services/storage.py` —— 源/音轨路径助手，删 pre_vc
- `backend/app/api/pipeline.py` —— `/trim`、`/restore-trim` 改为元数据
- `backend/app/api/voice.py` —— `/voice-convert`、`/voice-revert`
- `backend/worker/tasks.py` —— 生成时记录 fps/frames；VC 只产 wav；`run_merger` + `effective_clip_paths`
- `backend/app/agents/video_trimmer.py` —— 复用其帧精度抽帧
- `backend/app/agents/merger.py` —— 基本不动（吃 effective clip 路径）
- `frontend-vite/src/components/ShotPlayer.tsx`（新）、`useShotSync.ts`（新）、`ShotCard.tsx`（接入）
- 序列化层 —— 输出有效播放描述

## 9. 非目标 / YAGNI
- 区间裁剪（掐头去尾）—— 暂不做，单值 `trim_frames`。
- 多步撤销/重做历史栈 —— 当前语义是「还原到原始」，不做历史栈。
- CC 改为非破坏式 —— 不在本次范围。
- 向后兼容/数据迁移 —— 全量重构，不做。
