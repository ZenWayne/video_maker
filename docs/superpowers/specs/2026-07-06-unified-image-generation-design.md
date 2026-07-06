# 统一图片生成服务 — 设计文档

- **日期**: 2026-07-06
- **分支**: `worktree-unified-image-gen-spec`（off master）
- **状态**: 交互与架构均已用户确认；待写实现计划。
- **设计稿**: `design/shots.pen`（新增三块画板）；导出图 `design/exports/04-generate-image-dialog.png`、`05-keyframe-dropdown-generate.png`、`06-cc-candidates-strip.png`

## 背景与动机

后端存在 **3 个平行的 Gemini 图片调用点**，各自建 Vertex client、各自拼提示词、各自处理超时/空响应，无共享抽象（视频侧有 `VideoProvider` ABC，图片侧没有）：

1. **尾帧生成** `app/services/tail_frame_generator.py` — 两步 CoT（`tf_cot_prompt` 推结束姿势 → `tf_prompt` 生图），模型 `gemini-3.1-flash-image-preview`。
2. **CC 人脸校准** `app/services/face_calibration_client.py` — 对 shot 的 `last_frame` 做身份修复编辑（`cc_prompt`），直写替换。
3. **首帧生成** — `app/services/first_frame_generator.py`（已并入 master），复用尾帧生成器 helper，按 `visual_description` + 上下文帧 + 角色参考图生成首帧（`ff_cot_prompt`/`ff_prompt` 在 `config.py`；端点 `generate-first-frame`、worker `run_first_frame_pipeline` 均已存在）。

能力缺口：无法用自定义合成提示词生成；首帧生成不支持"尾帧→反推首帧"方向；参考图参与不可选（自动带 character）；生成结果直写槽位、无对比挑选。

## 已确认的产品决策

| 决策点 | 结论 |
|--------|------|
| 统一范围 | 后端服务层 + 统一生成 API + 前端入口，三层全做 |
| 提示词 | 统一一个入口：自定义提示词**可选**，留空走自动推理链（分镜提示词 + CoT）；填写则覆盖自动链直接生成 |
| 参考图 | 可勾选已有（项目级 character/scene + shot 级道具）+ **临时上传**（仅本次生成生效）；缺省自动带 character |
| 产物去向 | **一律先出候选**，用户采纳后才写入槽位 |
| 候选机制 | 每次生成 1 张；同一槽位候选累积成画廊，可对比/采纳/切换/删除；采纳后其余保留；**重新生成视频也不清理** |
| CC 收编 | 完全收编为统一服务的 edit 模式，且 **CC 也候选化**（校准结果先出候选，采纳才替换 last_frame） |
| 前端入口 | 复用关键帧管理下拉：首/尾帧组各加「生成…」项，打开统一生成弹窗；不新增按钮 |

## 交互设计（已画稿并确认）

### 统一生成弹窗（`04-generate-image-dialog.png`）

- **目标槽位**：首帧 / 尾帧分段切换，从下拉哪项进入就预选哪个；采纳的候选写入该槽位。
- **自动推理提示条**：提示词留空时的行为说明（如"分镜动作提示词 + 首帧 → 推导尾帧（两步 CoT）"，随槽位方向变化）。
- **自定义提示词**：可选 textarea；填写后覆盖自动推理，直接按用户合成/动作提示词生成。
- **参考图区**：已有参考图缩略图 + 勾选框（character 默认勾选；scene/道具可勾）；末位「临时上传」虚线格，上传图仅本次生成使用。
- **候选画廊**：按当前槽位展示累积候选；三种状态——已采纳（蓝框+徽标）、待采纳（采纳/删除）、生成中（spinner + 预估时长）。
- **底部**：异步提示（可关闭弹窗稍后回来采纳）+「生成 1 张候选」主按钮。

### 关键帧下拉（`05-keyframe-dropdown-generate.png`）

首帧组新增「生成首帧…」；尾帧组「生成尾帧」改为打开统一弹窗。其余项（上传/提取/用上一镜尾帧/删除）不变。

### CC 候选条（`06-cc-candidates-strip.png`）

校准完成后不再直写 `last_frame`：ShotCard 尾帧缩略图下方出现候选条——当前尾帧 → 候选对比 → 采纳/删除/再校准一次；注明采纳后保留 pre-CC 备份可还原。

## 架构

### 1. 服务层 `app/services/image_generation.py`

收编三个调用点，共享底座 + 四种模式：

- **共享底座**（从 `tail_frame_generator.py` 提炼）：Vertex `genai.Client` 单例（`vertexai=True`，禁 API key，遵守 CLAUDE.md）、`_call_with_timeout`、图片提取与空响应/block_reason 处理、`center_crop_to_aspect`、Langfuse 观测 span、CoT 弱输出重试（`_is_cot_too_weak` + 高温重掷）。
- **模式表**：

| 模式 | 输入 | 提示词链 |
|------|------|----------|
| `tail_frame` | 首帧（context）+ 参考图 + motion_prompt | 两步 CoT：`tf_cot_prompt` → `tf_prompt`（现有，含图片顺序约定：CoT 用 char+obj+frame，生图用 frame+obj+char） |
| `first_frame` | 尾帧/目标尾帧（context）+ 参考图 + visual_description 或 motion_prompt | 两步 CoT：新增 `ff_cot_prompt`/`ff_prompt` 到 `config.py`，从 `feat-generate-first-frame` worktree 原型改造 |
| `custom` | 自定义提示词 + 勾选参考图（+ 可选当前槽位帧作 context） | 单步直出，不走 CoT；用户提示词即最终提示词 |
| `cc_edit` | last_frame + character 参考图 | 现有 `cc_prompt`（编辑语义），单步 |

- 收编完成后**删除** `tail_frame_generator.py` 与 `face_calibration_client.py`；`worker/tasks.py` 全部改调统一服务。
- 不做 `ImageProvider` ABC（YAGNI：图片侧只有 Gemini/Vertex 一个 provider，与视频侧 vertex/kie 双 provider 不同）。

### 2. 数据模型 — `ImageCandidate` 表

```
id, project_id, shot_id,
slot            first_frame | tail_frame | cc
status          generating | done | failed
file_path       {shot_dir}/candidates/{ts_uuid}.png
prompt_source   auto | custom
custom_prompt   TEXT NULL
ref_paths       JSON（本次实际使用的参考图路径，含临时上传）
error           TEXT NULL
created_at, adopted_at NULL
```

- 文件目录：`storage.py` 新增 `shot_candidates_dir(project_id, shot_id)` → `{shot_dir}/candidates/`。
- 候选**永不自动清理**（重新生成视频不清）；随 shot/project 删除级联清理（行 + 目录）。
- 候选随 project 序列化按 shot 下发（`shots[].image_candidates`），前端画廊直接渲染。
- 现有 `tf_status` 字段保留兼容旧尾帧 spinner，前端切换到候选状态后按需退役（不在本设计强删）。

### 3. API（`app/api/pipeline.py` 或新 `images.py` 路由）

| 端点 | 行为 |
|------|------|
| `POST /projects/{pid}/shots/{sid}/image-candidates` (202, multipart) | 入参：`slot`（必填）、`custom_prompt?`、`ref_image_ids?`（勾选的已有参考图/道具）、`files[]?`（临时上传 → 存入 candidates 目录，仅本次引用）。建候选行（status=generating）→ 入队 ARQ `run_image_candidate` → 返回候选 id |
| `POST .../image-candidates/{cid}/adopt` | 按 slot 写槽位（见下"采纳语义"）；置 `adopted_at`；同槽位其他候选的 `adopted_at` 清空 |
| `DELETE .../image-candidates/{cid}` | 删行 + unlink；`status=generating` 时 409 拒绝；已采纳候选可删（槽位持有副本，不受影响） |

**采纳语义（与上传语义完全一致，路径即真相）**：

- `first_frame`：候选文件**复制**为 `custom_frames/{ts_uuid}` → 写 `custom_first_frame_path`。
- `tail_frame`：复制为 shot 目录 `{ts_uuid}.png` → 写 `target_last_frame_path`。
- `cc`：首次采纳先备份 `last_frame` → `last_frame_pre_cc.png`（沿用现有备份链），复制候选并将 `last_frame_path` 指向副本，`cc_status="done"`；现有"还原 pre-CC"能力不变。

**旧接口处理**：

- `generate-tail-frame` 改薄 wrapper：内部创建 tail_frame 候选（auto 模式），响应形状兼容；前端切换后删除。
- CC 批量校准端点（全部人物校准/单 shot 校准）改为逐 shot 产出 cc 候选，不再直写 last_frame。
- `confirm-tail-frame` 不受影响（尾帧确认+生视频耦合问题由 `2026-06-17-unify-shot-generation-api-todo.md` 另行处理）。

### 4. Worker — 单一 ARQ 任务 `run_image_candidate(project_id, shot_id, candidate_id)`

1. 读候选行，解析模式：`slot` + 有无 `custom_prompt`（cc 槽位固定 `cc_edit`；有 custom_prompt → `custom`；否则按 slot 方向选 `tail_frame`/`first_frame`）。
2. 自动模式下缺 `motion_prompt` 时先跑 director（沿用 `run_tail_frame_pipeline` 现逻辑）。
3. 解析 context 帧：tail_frame 模式用 `pick_first_frame()`；first_frame 模式用 `target_last_frame_path`（或已有视频 `last_frame_path`）。
4. 调统一服务生成 → 写 `file_path`、`status=done`；异常 → `status=failed` + `error`。
5. SSE 推事件（沿用 `services/events.py`）刷新前端候选状态。
6. **不触碰 project 状态机**：候选生成失败只影响该候选行，画廊内重试即可。

### 5. 前端（`ShotCard.tsx` + 新 `GenerateImageDialog.tsx`）

- 关键帧下拉加两个「生成…」项（预选槽位打开弹窗）；`api.ts` 新增 `createImageCandidate`（multipart）/`adoptImageCandidate`/`deleteImageCandidate`。
- 弹窗按设计稿实现；候选状态经 SSE/轮询刷新；CC 候选条嵌 ShotCard 尾帧缩略图下方（有待采纳 cc 候选时显示）。
- `types.ts` 增 `ImageCandidate`；shot 序列化带 `image_candidates`。

## 素材文件变更审计（遵循 CLAUDE.md）

- [x] 采纳 = **复制**候选文件到槽位命名空间（`custom_frames/{ts_uuid}` / shot 目录 ts_uuid），候选原件不动——删除候选不影响槽位，删除槽位不影响画廊。
- [x] 下游读取均经 DB 字段（`custom_first_frame_path`/`target_last_frame_path`/`last_frame_path`），candidates 目录不被任何下游硬编码读取。
- [x] cc 采纳沿用既有 `last_frame_pre_cc.png` 备份链与还原逻辑；`cc_status` 在采纳时置位，还原时按既有逻辑重置。
- [x] 尾帧候选采纳后 `tf_status` 置 `done`（兼容期），删除尾帧槽位走既有 `delete-tail-frame`（清字段 + unlink 槽位文件，候选不受影响）。
- [x] 临时上传只进 candidates 目录，不写入 `reference_images/`，不产生 `ReferenceImage` 行。
- [x] ts_uuid 命名保证每次写入 URL 唯一，前端无缓存问题。

## 错误处理

- 模型空响应/安全过滤：捕获 block_reason/finish_reason（沿用现有日志逻辑）→ 候选 `failed` + `error` 展示在画廊，提供重试（重试 = 新建候选）。
- 超时：`_call_with_timeout` 120s 转明确错误。
- 生成中候选禁删（409）；shot 删除时级联清 candidates。
- 弹窗可关闭，异步完成后经 SSE 更新，回来采纳。

## 测试（遵循：mock 所有模型调用；不 fake e2e 数据流）

### 后端单测（mock genai client）
1. 四种模式的提示词组装与图片 part 顺序（tail_frame 保持现有顺序约定；custom 不走 CoT）。
2. 候选 CRUD：创建 202 + 行 generating；删除 unlink；generating 禁删 409。
3. 三种 slot 的采纳：写对字段、复制而非移动、同槽位 adopted 互斥。
4. cc 采纳：首次备份 pre_cc、`last_frame_path` 指向副本、还原链不破。
5. 临时上传：文件只落 candidates 目录，无 `ReferenceImage` 行。
6. worker：auto 缺 motion_prompt 触发 director；失败置 failed 不动 project 状态。

### Playwright e2e
- 真实后端 + 真实 DB + 真实 adopt 流：只 stub `POST image-candidates`（AI 触发点，返回真实 202 形状）；候选行按 CLAUDE.md 方式直插 DB + 复制真实图片文件；断言真实 adopt 后 `GET /api/projects/{id}` 反映新的槽位路径、播放器/缩略图更新。

## 非目标（YAGNI）

- 不做 `ImageProvider` ABC / 多模型切换（图片侧仅 Gemini/Vertex）。
- 不做一次生成 N 张（每次 1 张，多次生成累积）。
- 不做跨 shot 的全局生成页。
- 不做候选自动清理策略/配额。
- 不动 `confirm-tail-frame` 与视频生成 API 统一（见 `2026-06-17-unify-shot-generation-api-todo.md`）。
- 不删 `tf_status` 列（兼容期保留）。

## 风险 / 注意

- CC 候选化改变现有"批量校准直接生效"的节奏：批量校准后需逐 shot 采纳。设计上以候选条集中呈现降低点击成本；若实际使用嫌繁琐，可后续加"全部采纳"批量按钮（暂不做）。
- 现有 `ff_` 提示词按"从 visual_description 正向生成"撰写，本设计要求同时支持"尾帧作 context 反推"场景，收编时需在 `ff_cot_prompt` 中补充方向性措辞。
- 旧 `run_tail_frame_pipeline` 迁移期间需保证 MCP 工具与现有前端不断链（wrapper 先行，前端切换后再删）。
