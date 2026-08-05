# 内容分析 → 创作简报 → 生成（垂直切片）设计文档

| 项目 | 内容 |
|---|---|
| 文档版本 | v1.0 |
| 状态 | 草稿 / 待评审 |
| 日期 | 2026-07-23 |
| 适用范围 | 「TikTok 爆款归因 → video_maker 生成」桥梁的第一个垂直切片 |
| 关联 | `docs/frds/Tiktok爆款归因分析工具frd.md`（母 FRD） |

---

## 1. 背景与定位

`docs/frds/Tiktok爆款归因分析工具frd.md` 定义了一个**独立的爆款归因分析工具**，其链路为「竞品账号 → 第三方数据 API → VAD/ASR → LLM 打标 → case-control 统计 → 归因报告」，并**明确把「内容生成 / 脚本创作」列为 Out of Scope**。

本设计要做的，正是母 FRD 刻意不做的那座桥：把归因分析的思路引入 `video_maker`（一个 AI 视频生成工具），并与其生成能力耦合，形成 **分析 → 创作简报（brief）→ 喂给 screenwriter 生成分镜** 的闭环。

### 1.1 整体是多子系统，本文档只覆盖第一个切片

完整目标 ≈「几乎整份 FRD + 一个生成桥梁」，涉及 7 个相对独立的子系统，远超单个实现计划的承载量，且其中「第三方数据 API 采集」当前卡在供应商未签约（母 FRD OI-1）。因此采用**子项目拆分 + 分阶段**策略，本文档只把**第一个垂直切片**做成完整 spec：

| # | 子系统 | 对应 FRD | 是否本切片 |
|---|--------|----------|-----------|
| 1 | 采集层（adapter + `VideoRecord` + 爆款/对照标记） | FR-1 | 否（后续子项目） |
| 2 | 音频下载 → VAD → ASR → hook 切分 | FR-3 | **基本覆盖**（上传视频 + 本地 faster-whisper-large-v3，含内置 VAD 与词级时间戳） |
| 3 | caption 解析 + 逐视频结构化打标 | FR-2 / FR-4 | 否（本切片不做，改为对转写文本整体归纳，见 §1.3） |
| 4 | case-control 统计检验 | FR-5 | 否（本切片不做对照/统计，见 §1.2） |
| 5 | 归因报告 | FR-6 | 部分（产物是 brief，非报告） |
| 6 | 创作简报（brief） | 无（新增） | **是** |
| 7 | brief → 编剧生成桥梁 | 无（新增） | **是** |

### 1.2 与母 FRD 的关键偏离：不做对照组

母 FRD 的核心原则是「**有对照组的归因才是归因**」。本切片**按用户明确要求去掉对照组**，只做「AI 辅助的爆款共性分析」。

诚实标注其代价：去掉对照组后，产物本质是「总结爆款有哪些**共性特征**」，而非「哪些特征**真正区别于**不火的内容」——这正是母 FRD 反复警告的「正确但难落地」那类结论。但对本切片的用途（AI 辅助找创作灵感 → 喂给生成）而言，这一取舍可接受，且工程显著更轻。此偏离是**有意为之**，不是遗漏。

### 1.3 关键偏离二：只分析口播转写文本，不做结构化打标 / caption

**按用户明确要求**，本切片**只分析「音频转写出来的口播文字」**，砍掉两块：

- **逐视频结构化特征打标**（母 FRD FR-4 的 `hook_type` / `emotion` / `cta_type` 等分类字段 + `caption_len` / `emoji_count` 等代码直算字段）——不再有独立打标步骤、不落 `tags_json`。
- **caption（视频描述文案）**——caption 不是音频转写文本，本切片不采集、不分析。

归因改为**一次 LLM 直接读所有样本的转写文本**（`full_transcript` + `hook_text`），整体归纳出 brief。hook（前 3 秒口播）仍保留——它属于「音频转写文本」，且 faster-whisper 的词级时间戳白给、FRD 视其为最强信号。

## 2. 目标与非目标

### 2.1 本切片目标

1. 用户在 video_maker 内新建一个「内容分析」，**一次上传多条爆款视频**，联合归因，产出一份结构化 **creation brief**。
2. 后端对每条视频抽音频、用本地 faster-whisper-large-v3 转写口播、按词级时间戳**精确**切出 hook（`start < 3.0s`）。**只产出转写文本，不做结构化打标**（§1.3）。
3. 一次 LLM 直接读所有样本的转写文本，**联合归纳共性制胜模式**，生成 brief。
4. brief 可**快照挂载**到新建 project；`screenwriter` 生成分镜时注入该 brief。
5. 全程异步（ARQ worker）+ SSE 进度，复刻现有 `Project → Shot` 生成流程的架构范式。

### 2.2 明确 Out of Scope（映射到后续子项目）

- 第三方数据 API 采集（母 FRD FR-1）
- **对照组 / case-control 统计**（卡方、p 值、效应量，母 FRD FR-5）
- **逐视频结构化特征打标（母 FRD FR-4）+ caption 解析（母 FRD FR-2）**——只分析口播转写文本（§1.3）
- 多账号赛道级横向汇总（母 FRD FR-5.5）
- 屏幕文字 OCR（母 FRD §6.3）
- hashtag 大小标签分档（母 FRD FR-2.4）
- 报告 PDF 导出（母 FRD FR-6.6）

## 3. 架构总览

复用 video_maker 现有基建，不引入新范式：

- **实体组织**：`ContentAnalysis`（新顶层实体，与 `Project` 平级）→ `ReferenceSample`（子表，类比 `Shot`）。
- **异步执行**：ARQ worker（现有 `backend/worker/`），3 步状态机。
- **进度推送**：redis pubsub + SSE（现有 `app/services/events.py`），新增一条 analysis 事件 channel。
- **LLM**：现有 `GeminiProvider`（Vertex，service account，非 API key），仅用于 brief 归纳（**不用于转写、不做结构化打标**）。
- **ASR**：本地 **faster-whisper-large-v3**（HF `Systran/faster-whisper-large-v3`，CTranslate2 实现），内置 Silero VAD + 词级时间戳。新增依赖到 `backend/pyproject.toml`，跑在 worker 里，模型权重经缓存卷持久化（见 §12 部署）。ASR 封在一个小接口后，便于日后按母 FRD FR-3.7 切换云 API。
- **媒体**：现有 `app/agents/audio_extractor.py::extract_audio_wav`（ffmpeg 抽音频）。
- **存储**：现有 storage service，新增 `storage/analyses/{analysis_id}/...` 目录。

```
用户上传 N 条爆款视频
        │
        ▼  [API] 建 ContentAnalysis + N×ReferenceSample，入队
[worker] transcribing：逐样本 抽音频→faster-whisper(VAD+词级时间戳)→精确 hook→删 wav
        ▼
[worker] analyzing：1×LLM 读所有转写文本(full_transcript+hook_text)联合归纳 → creation brief
        ▼
brief_json 落库 → SSE done
        │
        ▼  建 project 时挂载 brief（快照）→ screenwriter 注入 → 生成分镜
```

## 4. 数据模型

### 4.1 新表 `content_analyses`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | String(36) uuid | 主键 |
| `title` | Text | 用户命名，如「美妆赛道-账号A」 |
| `region_hint` | Text nullable | 目标市场/语言预设（母 FRD FR-3.6，避免短音频自动语言误判） |
| `status` | String(20) | `uploading` → `transcribing` → `analyzing` → `completed` / `failed` |
| `brief_json` | Text nullable | 最终 creation brief（结构化 JSON），完成时写入 |
| `error_message` | Text nullable | |
| `created_at` / `updated_at` | DateTime | |

### 4.2 新表 `reference_samples`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | Integer autoincrement | 主键 |
| `analysis_id` | String(36) FK → content_analyses (CASCADE) | |
| `order_index` | Integer | 展示顺序 |
| `video_path` | Text | 上传的参考视频 |
| `audio_path` | Text nullable | 抽出的 wav，**转写后即删**（母 FRD NFR-2.1，仅中间产物） |
| `has_speech` | Boolean nullable | 无人声检测结果（母 FRD FR-3.3） |
| `hook_text` | Text nullable | 前 3s 内的词（词级时间戳精确切分，母 FRD FR-3.5） |
| `full_transcript` | Text nullable | 完整口播转写 |
| `language` | String(10) nullable | 转写语言 |
| `status` | String(20) | `pending` → `transcribing` → `transcribed` → `failed` |
| `error_message` | Text nullable | 逐样本失败记录，不静默丢弃（母 FRD FR-1.5） |
| `created_at` | DateTime | |

> **注**：无 `label`（viral/control）字段——本切片所有样本都是待分析的爆款，不做对照（§1.2）。

### 4.3 `projects` 表新增字段（挂钩 brief）

| 字段 | 类型 | 说明 |
|---|---|---|
| `content_analysis_id` | String(36) nullable | 溯源：本 project 挂载的分析 id |
| `attached_brief_json` | Text nullable | **brief 快照**——挂载时把 brief 拷进 project，日后分析/brief 改动不回溯污染已建 project |

> 快照而非外键引用：保证「挂载那一刻」的 brief 被冻结，符合 video_maker 素材版本纪律的一贯风格。

### 4.4 存储布局

```
storage/analyses/{analysis_id}/
  samples/{sample_id}/
    source.mp4        # 上传的源视频（分析期间保留）
    audio.wav         # 派生音频，转写后删除（NFR-2.1）
```

## 5. 分析管线（ARQ worker，3 步状态机）

### Step 1 — uploading（API 侧，入队前）

- 建 `ContentAnalysis`（title + 可选 region_hint）。
- 逐条上传视频 → 建 `ReferenceSample`。
- **软下限**：样本数 ≥ 3 才够出一份有意义的 brief；不足则在 brief 的 `sample_stats.sample_warning` 标注「样本偏少，仅供参考」（母 FRD FR-1.7 精神），**不硬拦**。
- 入队 analysis job，status → `transcribing`。

### Step 2 — transcribing（逐样本）

对每条 `ReferenceSample`（faster-whisper-large-v3，`vad_filter=True` + `word_timestamps=True`）：
1. `extract_audio_wav(video_path)` → `audio.wav`（16k mono，Whisper 友好）。
2. `WhisperModel.transcribe(audio, vad_filter=True, word_timestamps=True, language=<按 region_hint 预设>)`。`language` 仅在账号标记为多语言时才走自动检测（母 FRD FR-3.6）。
3. **无人声判定（母 FRD FR-3.3）**：内置 Silero VAD 过滤后若无有效语音段 → `has_speech=false`，`full_transcript`/`hook_text` 留空、不进归因。VAD 同时抑制纯 BGM 幻觉文本。
4. 产出 `full_transcript`（拼接 segments）；`hook_text` = 所有 `word.start < 3.0` 的词（母 FRD FR-3.5，**精确**切分）；`language` = `info.language`。
5. **删 `audio.wav`**（`finally` 保证，异常路径也不残留）。
6. sample.status → `transcribed`（或 `failed` 带因）。

> 模型实例在 worker 进程内单例加载（避免每条样本重载 ~3GB 权重）。dev 用 `device=cpu, compute_type=int8`；有 GPU 时可切 `cuda/float16`（配置项）。

### Step 3 — analyzing（1× LLM，跨样本联合归因）

- 输入：**所有有人声样本的转写文本** `full_transcript` + `hook_text`（无结构化打标、无 caption）。
- **不做组间对比**，而是让 LLM 通读所有爆款口播文本，**联合归纳共性制胜模式**：开场钩子套路、语速/情绪、信息缺口、CTA 等——这些由 LLM 从转写文本里直接读出，不落逐视频结构化字段。
- 输出 creation brief（§6 schema）。含 `no_speech_pct` 覆盖度披露（母 FRD FR-6.5 精神）与样本量警告。
- `brief_json` 落库，analysis.status → `completed`，SSE done。

### 进度事件

全程走现有 redis pubsub / SSE 基建，新增 analysis channel（`get_channel_name` 复用同一模式，key 用 analysis_id）。前端订阅得到每步 + 逐样本状态。

## 6. Creation Brief Schema

桥梁产物，从母 FRD FR-6 报告演化为「生成导向」，**无对照字段**：

```json
{
  "niche_summary": "赛道/账号一句话画像",
  "sample_stats": {
    "sample_n": 5,
    "no_speech_pct": 0.0,
    "sample_warning": "样本偏少，仅供参考|null"
  },
  "hook_strategy": {
    "common_hook_types": ["疑问", "数字承诺"],
    "example_hooks": ["真实 hook 示例1", "示例2"]
  },
  "script_structure": {
    "pacing": "语速偏快",
    "emotion": "争议/正向",
    "info_gap": "制造信息缺口",
    "cta": "引导评论"
  },
  "do": ["可执行的做法1", "..."],
  "dont": ["要避免的做法1", "..."],
  "screenwriter_directives": "一段可直接注入 screenwriter 的中文创作指令"
}
```

> 所有字段均由 §5 Step 3 的单次 LLM **从口播转写文本直接归纳**，无逐视频结构化打标中间层，无 caption 相关字段。

## 7. Screenwriter 注入

- `run_screenwriter(...)` 增加可选参 `creation_brief: Optional[dict] = None`（**向后兼容**，现有调用不传即原样行为）。
- 有 brief 时，在 system/user prompt 追加一段「赛道爆款简报（务必据此创作）」：主体用 `screenwriter_directives`，附上 `hook_strategy` / `script_structure` 要点。
- project 挂了 brief（`attached_brief_json` 非空）时，scripting 流程从**快照**读取并传入 `run_screenwriter`。
- brief 结论用于**引导**创作方向，不覆盖用户 `theme_text` 与参考图这两个既有输入。

## 8. 无人声降级与错误处理

- **无人声（母 FRD FR-3.3）**：faster-whisper 内置 Silero VAD（`vad_filter=True`）过滤后无有效语音段 → `has_speech=false`、不进归因、计入 brief 的 `no_speech_pct`；VAD 同时抑制纯 BGM 幻觉文本。
- **逐样本失败不拖垮整体（母 FRD FR-1.5）**：单条转写失败 → 标 `failed` + 记因，其余继续。成功样本 ≥ 1 即可进 analyzing；若 0 条可用 → analysis `failed` 带明确文案。
- **brief LLM JSON 解析失败（母 FRD FR-4.2 精神）**：重试 + 记录；仍失败则 analyzing `failed`。
- **audio 清理**：`finally` 里删 wav，异常路径不残留（母 FRD NFR-2.1）。

## 9. 测试策略

遵守 `CLAUDE.md`：**只在计费模型边界打桩，绝不伪造被测流程**。ASR 是本地 faster-whisper、**不计费**，因此转写不属于「必须打桩」的边界——**唯一计费边界是 brief 归纳的那次 LLM 调用**。

- **单元**：VAD 无人声判定、词级时间戳 → hook 切分（`start < 3.0s`）、brief → screenwriter prompt 渲染。
- **集成**：analysis worker 全流程；**仅在 brief LLM 边界打桩**（返回预制 brief），真 DB、真状态迁移、真 ffmpeg 抽音频。转写可用**真 faster-whisper 跑一条极小含语音 fixture**（本地免费、且验证真链路），或为提速用 `tiny` 模型 / 桩 ASR（二选一，按 CI 时长权衡）。断言真实 `ContentAnalysis` 行 + `ReferenceSample` 状态 + `brief_json`。
- **E2E（Playwright）**：真后端 / 真 DB / 真端点。上传真 fixture 视频，**只短路 brief 归纳这一个计费调用**（在 model 边界，返回其真实响应形状），断言真实 analysis 行 → brief → 挂到 project → screenwriter 收到 brief。**绝不** `route.fulfill` 分析数据本身。

## 10. 前端（设计层）

新增「内容分析」入口，复用现有 project list / SSE 进度组件：
- 分析列表页。
- 建分析：title + 可选 region_hint。
- 上传样本：多视频上传。
- 进度视图：SSE 展示 3 步 + 逐样本转写状态。
- brief 展示：结构化渲染 §6 schema。
- 新建 project 时：新增「挂载 brief」选择器，选中即把该分析的 `brief_json` 快照进 project。

## 11. 后续子项目（本切片之后）

1. **采集层**（母 FRD FR-1）：adapter + `VideoRecord` + 第三方数据 API（待 OI-1 供应商选型）——接入后可用账号 handle 自动拉全量视频，替代手工上传。
2. **对照组 + case-control 统计**（母 FRD FR-5）：恢复 FRD 灵魂原则，出带 p 值/效应量的显著性结论。
3. **多账号赛道级汇总**（母 FRD FR-5.5）。
4. **ASR 引擎可切换**（母 FRD FR-3.7）：本切片已本地 faster-whisper；后续可加云 API 选项供大批量时权衡成本。

每个子项目各自走 spec → plan → 实现。

## 12. 部署与依赖（faster-whisper）

- **依赖（独立分组）**：`faster-whisper` 放进 `backend/pyproject.toml` 的**独立 `[dependency-groups].asr` 组**，不进主 `dependencies`——仅 worker 需要、且体积大（CTranslate2 / onnxruntime / av）。安装：`uv sync --group asr`。worker 容器装 `asr` 组，backend(API) 容器不装。
- **模型权重**：`Systran/faster-whisper-large-v3` ≈ 3GB，首次运行自动从 HF 下载到缓存目录。**新增一个命名卷挂 HF 缓存**（如 `deploy_whisper-cache` → 容器内 `HF_HOME`），避免每次重建容器重下。
- **加载**：worker 进程内单例（模块级懒加载），避免逐样本重载。
- **算力**：dev 默认 CPU `int8`（slim 镜像即可，注意 ffmpeg 与常见 `libgomp` 依赖已随现有镜像具备）；有 GPU 时经配置切 `cuda/float16`。
- **配置项**：`ASR_MODEL`（默认 `large-v3`）、`ASR_DEVICE`（默认 `cpu`）、`ASR_COMPUTE_TYPE`（默认 `int8`）——为母 FRD FR-3.7 的引擎可切换预留。
- **合规**：仅本地推理，音频为中间产物、转写后即删（母 FRD NFR-2.1），不出网、不落第三方。
