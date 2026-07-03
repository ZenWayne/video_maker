# 首帧 AI 生成（支持参考物）— 设计文档

- **日期**: 2026-07-03
- **分支**: `worktree-feat-generate-first-frame` (off master)
- **状态**: 后台自主任务产出——设计假设未经用户逐条确认，全部镜像既有尾帧（tail frame）生成模式；假设清单见文末。

## 背景

需求：「首帧也需要图片生成，像尾帧一样，支持参考物」。

尾帧已有完整生成链路：`POST .../generate-tail-frame` → ARQ `run_tail_frame_pipeline` →
`tail_frame_generator.generate_tail_frame`（Vertex Gemini 图像模型，两步 CoT），输入 =
项目角色参考图（identity）+ shot 级参考物 `custom_reference_paths`（props）+ 首帧（场景上下文）。

首帧（`custom_first_frame_path`，路径即真相）目前只有 上传 / 提取本镜首帧 / 用上一镜末帧 / 删除，
**没有 AI 生成入口**。本设计补齐它。

一个直接收益：Veo 的多图参考模式与首/尾帧模式互斥（video_generator.py）。把参考物
"烘焙"进 AI 生成的首帧后，既能带道具，又能继续使用首帧 image-to-video + 尾帧插值。

## 方案：完全镜像尾帧链路

### DB（`app/models/project.py` Shot）

新增两列，镜像 `tf_status` / `tf_error_message`：

| 列 | 类型 | 语义 |
|---|---|---|
| `ff_status` | VARCHAR(20) | null \| "generating" \| "done" \| "failed"（瞬时进度，不参与决策） |
| `ff_error_message` | TEXT | 生成失败原因 |

`db.py` 按现有 PRAGMA + ALTER TABLE 模式加迁移。`schemas.py` `ShotResponse`、
`stream.py` SSE 序列化、前端 `types.ts` 同步。

**不新增路径字段**：产物直接写现有单一真相 `custom_first_frame_path`。

### 生成服务（`app/services/first_frame_generator.py`）

镜像 `tail_frame_generator.py` 两步 CoT，复用其 `_get_client` / `_mime_for` /
`_call_with_timeout` / `_extract_text`（同一 Vertex client、同一 `tf_model`/`tf_cot_model`）：

- **Step 1（TEXT CoT）**：从 `visual_description`（主）+ `motion_prompt`（辅，若有——开场
  应为动作的起点）推导「开场构图」：机位/景别（`shot_type`）、人物起始姿态、参考物摆放。
- **Step 2（IMAGE）**：图片顺序 `context_parts + obj_parts + char_parts`（与尾帧同理，
  character identity 放最后保持强条件）。参考物必须清晰可见；角色 identity 来自角色参考图；
  场景上下文图仅用于背景/光线/服装的连贯性。
- 产物 `center_crop_to_aspect` 到项目宽高比。

新增 config：`ff_cot_prompt`、`ff_prompt`（模型/项目/位置复用 `tf_*` 设置）。

**场景上下文图（context frame）**：取 `pick_first_frame(...)` 的当前解析结果（现自定义首帧 /
上一镜末帧 / 角色参考图），可为 None（多图参考模式）。作用等价于尾帧生成里的 Starting Frame。

### Worker（`worker/tasks.py` `run_first_frame_pipeline`）

镜像 `run_tail_frame_pipeline`：

1. SSE `ff_started`。
2. `context_frame = pick_first_frame(...)`；`obj_refs = json.loads(custom_reference_paths)`；
   `char_refs = _get_character_ref_paths(...)`。**不跑 director**（首帧不需要 motion_prompt，
   已有则仅作为 CoT 辅助输入）。
3. 产物写 `shot_custom_frames_dir(...)/{ts_uuid}.png` → `custom_first_frame_path`。
   - ts_uuid 名：URL 唯一、天然防缓存（与上传/提取一致）。
   - 位于 `custom_frames/` ⇒ `_propagate_first_frame_to_next` 视为用户覆盖，不会被上一镜
     重新生成时自动覆写——AI 生成的首帧和上传一样是明确的用户选择。
4. 成功：`ff_status="done"`、清 `ff_error_message`、SSE `ff_completed`（带 media URL）。
   失败：`ff_status="failed"` + `ff_error_message`、SSE `ff_failed`。两者都恢复
   `shot.status=PENDING` 并 transition 回 `SHOT_REVIEW`。

### 端点（`app/api/pipeline.py`）

```
POST /projects/{pid}/shots/{sid}/generate-first-frame   → 202 {status:"queued", shot_id}
```

镜像 `generate-tail-frame`：置 `ff_status="generating"`、transition `SHOT_GENERATING`、
入队 `run_first_frame_pipeline`。无请求体。

对称性小改：`DELETE .../first-frame` 增加 `ff_status=="generating"` 时 409 守卫
（与 delete-tail-frame 一致），并在删除时清 `ff_status`/`ff_error_message`。

### 前端

- `ShotCard.tsx` 首帧 `KeyframeSlot`：菜单加「生成首帧」；`generating`/`failed` 由
  `ff_status` 驱动；失败可重试（再次调用生成）。
- `api.ts` `generateFirstFrame(projectId, shotId)`；`ShotsPage.tsx` `handleGenerateFirstFrame`；
  `useShotSync.ts` 处理 `ff_started`/`ff_completed`/`ff_failed`（镜像 `tf_*`）。
- Playwright/测试规则不变：`generate-first-frame` 属于 AI 触发端点，e2e 只允许
  short-circuit 成真实 202 形状。

## 素材文件变更审计（CLAUDE.md）

- [x] 产物只写 `custom_first_frame_path`（DB 字段），下游 `pick_first_frame` 唯一读点，无硬编码文件名。
- [x] 新文件 ts_uuid 命名于 `custom_frames/`——与上传/提取同目录同规则；`_propagate_first_frame_to_next`
      的 "custom_frames" 用户覆盖判定自动保护生成结果。
- [x] 不触碰 `output*.mp4` / `last_frame*.png` / 备份文件 / `vc_status` / `cc_status`——首帧生成
      不改变已生成视频。
- [x] 旧首帧文件不主动 unlink（与 upload-first-frame 行为一致；删除入口统一走 DELETE first-frame）。

## 测试（mock 所有模型调用）

1. 端点：202、`ff_status="generating"`、入队 `run_first_frame_pipeline`、非法状态 409。
2. worker 成功路径（mock `generate_first_frame`）：文件落 `custom_frames/` ts_uuid 名、
   `custom_first_frame_path` 更新、`ff_status="done"`、SSE `ff_completed`、回 `SHOT_REVIEW`。
3. worker 失败路径：`ff_status="failed"` + `ff_error_message`、SSE `ff_failed`、回 `SHOT_REVIEW`。
4. 参考物注入：`custom_reference_paths` 存在时作为 `object_ref_paths` 传入生成器。
5. 生成后传播保护（回归）：生成的首帧在上一镜重新生成后不被 `_propagate_first_frame_to_next` 覆写。
6. DELETE first-frame：generating 时 409；删除清 `ff_status`。

## 非目标（YAGNI）

- 不加 `ff_confirmed`（keyframe 设计已废除确认环节：路径有了就生效）。
- 不改 director / motion_prompt 流程。
- 不做首帧生成的自定义 prompt 输入框（尾帧也没有；如需后续再加）。
- 不改多图参考模式语义（生成首帧后自然回到 image-to-video，是预期行为）。

## 自主决策的假设清单（供审阅）

1. 产物写 `custom_frames/{ts_uuid}.png` 而非固定名——为获得传播保护 + 防缓存。
2. 生成输入 = 角色参考图 + shot 参考物 + `visual_description`（+ `motion_prompt` 辅助）+
   当前解析首帧作场景上下文；复用 `tf_model`/`tf_cot_model`。
3. 两步 CoT 结构与尾帧一致（先推导开场构图，再出图）。
4. 「生成首帧」按钮常显（`visual_description` 非空列，无禁用条件）。
5. 不删旧首帧文件、不加确认环节、不发 CoT 中间 SSE 事件。
