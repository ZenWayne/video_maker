# 设计：尾帧可删除功能

日期：2026-06-21
目标分支：feat/kie-ai-video-provider（功能依赖该分支当前未提交的尾帧改动）

## 背景

每个 shot 可以生成一张「目标尾帧」(`target_last_frame.png`)，用于视频生成时约束最后一帧、并配合 auto-trim 做镜头衔接。当前尾帧的生命周期为：

- 生成：`POST .../generate-tail-frame` → worker `run_tail_frame_pipeline`
- 确认：`POST .../confirm-tail-frame` → 确认后用尾帧生成视频
- 从视频提取：`POST .../extract-tail-frame` → 把视频实际末帧设为目标尾帧

后端**已存在** `POST .../delete-tail-frame` 端点（`backend/app/api/pipeline.py:850`，当前在未提交工作区改动中），但它的行为是「删除尾帧 + 立即用首帧生成视频」，且**前端完全没有接入**（`api.ts` 无 `deleteTailFrame`，UI 无删除入口）。

## 目标

让用户能在前端删除一个已生成的尾帧，删除后 shot 回到「可直接用首帧出视频」的中性状态，**不自动触发视频生成**。「生成尾帧」入口始终可用，删除后可再次生成（首次或覆盖）。

## 非目标

- 不做软删除/可撤销恢复（删除即清空，要恢复就重新生成）。
- 不改动 `generate-tail-frame` / `confirm-tail-frame` / `extract-tail-frame` 的现有**行为**。（注：`generate-tail-frame` 会与 `delete` 共用 `_reset_tail_frame` 助手做**行为等价**的重构 —— 仅实现简化，行为不变。）
- 不引入单独的「仅生成本 shot 视频」按钮（超出范围）。

## 相关数据模型

`Shot` 表（`backend/app/models/project.py`）尾帧相关字段：

| 字段 | 含义 |
|------|------|
| `skip_tail_frame` (bool, 默认 False) | 用户选择跳过尾帧，视频仅用首帧 |
| `target_last_frame_path` (text, nullable) | 目标尾帧图片路径 → 磁盘 `target_last_frame.png` |
| `tf_status` (str, nullable) | `null` \| `generating` \| `done` \| `failed` |
| `tf_error_message` (text, nullable) | 失败原因 |
| `tf_confirmed` (bool, 默认 False) | 用户已确认尾帧，可用于视频生成 |

磁盘路径：`shot_target_last_frame_path(project_id, shot_id)` → `.../shots/shot_{id}/target_last_frame.png`。

## 设计

### 1. 后端：改造 `delete_tail_frame`（`pipeline.py:850`）

将端点行为从「删除并出视频」改为「仅删除」：

**保留：**
- 404 校验（project / shot 不存在）
- 409 校验：`tf_status == "generating"` 时拒绝删除（正在生成中）
- 清空尾帧状态：调用统一助手 `_reset_tail_frame(shot, skip=True)`（`pipeline.py`），一次性置 `tf_status=None` / `tf_confirmed=False` / `target_last_frame_path=None` / `tf_error_message=None` / `skip_tail_frame=True`，避免在每个端点重复罗列 5 个字段（见下「实现：`_reset_tail_frame` 助手」）。

**新增：**
- 物理删除磁盘上的 `target_last_frame.png`（若存在）。符合 CLAUDE.md「Shot 素材文件变更审计」：清理变更的素材文件，避免下游读到过期文件。用 `shot_target_last_frame_path(project_id, shot_id)` + `Path.unlink(missing_ok=True)`。

**移除：**
- `transition_project_status(project, ProjectStatus.SHOT_GENERATING, ...)` —— 不再切状态，项目停留在当前状态（`SHOT_REVIEW`）。
- `arq.enqueue_job("run_shot_pipeline", ...)` —— 不再自动出视频。

**返回：**
```json
{ "shot_id": <id>, "skip_tail_frame": true, "tf_status": null }
```
状态码：改造后端点不再排队，建议改为 `200`（更语义化）。保持 `202` 亦可，由实现计划定夺，但需同步前端对返回的处理（前端不依赖具体码，仅依赖 2xx）。

**对称性说明：** 删除与重新生成共用 `_reset_tail_frame(shot, skip=...)`：`generate-tail-frame` 用 `skip=False`（重新启用尾帧流程），随后单独置 `tf_status="generating"`；`delete-tail-frame` 用 `skip=True`（中性删除，仅用首帧出视频）。`skip_tail_frame` 是 `_shot_needs_tail_frame()`（`pipeline.py:44`）路由「首帧 worker / 尾帧 worker」的唯一开关 —— 此处「尾帧流程」即 `_shot_needs_tail_frame → run_tail_frame_pipeline` 这条路径，故两端拨同一布尔位天然对称。删除后 `tf_status=None` 还使前端「生成尾帧」按钮（`!tf_status` 条件，`ShotCard.tsx:954`）重新出现，点击即覆盖重生成。

**实现：`_reset_tail_frame` 助手（方案 A）** —— `delete` 与 `generate` 原本各自罗列 5 行尾帧字段置位，抽出统一助手消除重复：

```python
def _reset_tail_frame(shot: Shot, *, skip: bool) -> None:
    """Clear a shot's tail-frame state in one place.

    skip=True  → 中性删除：清空尾帧，不再自动走尾帧流程（仅用首帧出视频）。
    skip=False → 重新启用尾帧流程（重新生成）。
    """
    shot.tf_status = None
    shot.tf_confirmed = False
    shot.target_last_frame_path = None
    shot.tf_error_message = None
    shot.skip_tail_frame = skip
```

- `delete-tail-frame`：`_reset_tail_frame(shot, skip=True)`（5 行 → 1 行）
- `generate-tail-frame`：`_reset_tail_frame(shot, skip=False)` + `shot.tf_status = "generating"`（5 行 → 2 行，行为不变）
- `approve-script`（不动 `skip`）、`regenerate-shots`（按文件存在与否条件保留尾帧）语义不同，**不套用**此助手。
- 范围内仅做去重，不折叠字段；把 `tf_confirmed` / `skip_tail_frame` 并入 `tf_status` 枚举（5 列 → 3 列）涉及 DB 迁移 + 前端读取点，列为后续独立重构。

### 2. 素材审计（CLAUDE.md「Shot 素材文件变更审计」要求）

删除操作修改素材文件（`target_last_frame.png`），按审计清单核对：

- [x] 下游视频生成通过 `shot.target_last_frame_path`（DB 字段）读取；删除后置 `None`，视频生成自动走首帧分支 —— 无硬编码文件名问题。
- [x] `target_last_frame.png` **无备份链**（不像 `output_original.mp4` / `output_pre_vc.mp4`），无需清理其他备份。
- [x] `auto_trim_to_tail_frame()` 仅在 `tf_confirmed=True` 时执行；删除后 `tf_confirmed=False`，不会误用。
- [x] `tf_status` / `tf_error_message` / `tf_confirmed` 均已重置。
- [x] CC（人物校准）不受影响，无需重置 `cc_status`。

结论：删除仅清空尾帧本身，无过期文件风险。

### 3. 前端：API 客户端（`frontend-vite/src/lib/api.ts`）

新增（紧邻 `extractTailFrame`，约 `api.ts:370`）：

```typescript
deleteTailFrame: (projectId: string, shotId: number): Promise<{ shot_id: number; skip_tail_frame: boolean; tf_status: null }> => {
  return request('POST', `/api/projects/${projectId}/shots/${shotId}/delete-tail-frame`)
},
```

### 4. 前端：ShotsPage 处理函数（`frontend-vite/src/pages/ShotsPage.tsx`）

新增（紧邻 `handleExtractTailFrame`，约 `ShotsPage.tsx:496`）：

```typescript
// 删除尾帧
const handleDeleteTailFrame = async (shotId: number) => {
  if (!projectId) return
  try {
    await api.deleteTailFrame(projectId, shotId)
    updateShot(shotId, {
      tf_status: null,
      tf_confirmed: false,
      target_last_frame_path: null,
      skip_tail_frame: true,
    })
    addToast({ type: 'success', message: `镜头 #${shotId} 尾帧已删除` })
  } catch (error) {
    addToast({ type: 'error', message: error instanceof Error ? error.message : '删除失败' })
  }
}
```

**注意：** 不调用 `setStatus(...)`，project 状态不变。

将 `onDeleteTailFrame={status !== 'script_review' ? handleDeleteTailFrame : undefined}` 传给 `<ShotCard>`（与 `onGenerateTailFrame` 的两处传参位置一致：`ShotsPage.tsx:601` 与 `:756`，确保两处都补上）。

### 5. 前端：ShotCard UI（`frontend-vite/src/components/ShotCard.tsx`）

新增 prop：`onDeleteTailFrame?: (shotId: number) => void`（在 props interface `:43` 附近及解构 `:91` 附近补上，与 `onGenerateTailFrame` 并列）。

在三个尾帧存在的状态加「删除尾帧」按钮（红色 ghost），点击前弹二次确认：

- **`tf_status === 'done' && !tf_confirmed`**（`:677-713`）：加在「确认并生成视频 / 重新生成」那一排末尾。
- **`tf_status === 'done' && tf_confirmed`**（`:715-736`）：加在「重新生成尾帧」旁。
- **`tf_status === 'failed'`**（`:738-758`）：加在「重试」旁，让用户可放弃失败的尾帧。

**二次确认（已确认要加）：** 实现计划阶段先确认本仓库已有的确认弹窗组件/约定（搜索 `ConfirmDialog` / `window.confirm` / 已有 toast 确认模式），按现有约定实现，**不新造组件**。确认文案：「确定删除该镜头的目标尾帧？删除后需重新生成。」

按钮示例（确认包装按现有约定）：
```tsx
{onDeleteTailFrame && (
  <Button variant="ghost" size="sm" className="h-7 px-2 text-xs text-red-600 hover:text-red-700"
    onClick={() => confirmThenDelete(shot.shot_id)}>
    <Trash2 className="w-3 h-3 mr-1" />删除尾帧
  </Button>
)}
```
（`Trash2` 来自 `lucide-react`，与现有图标导入一致。）

## 测试

后端集成测试（`backend/tests/integration/test_pipeline.py`），更新或新增 delete-tail-frame 用例。按 CLAUDE.md：AI 触发端点需 mock，但 delete-tail-frame 改造后**不再** enqueue worker，故无需 mock AI，只需断言不排队。

断言：
1. 前置一个 `tf_status='done'`、`target_last_frame_path` 指向真实临时文件、`tf_confirmed=True` 的 shot，project 处于 `SHOT_REVIEW`。
2. 调用后：`tf_status is None`、`tf_confirmed is False`、`target_last_frame_path is None`、`tf_error_message is None`、`skip_tail_frame is True`。
3. 磁盘 `target_last_frame.png` 已删除。
4. **未** enqueue `run_shot_pipeline`（断言 arq 队列为空 / mock 未被调用）。
5. project 状态仍为 `SHOT_REVIEW`（未变为 `SHOT_GENERATING`）。
6. `tf_status='generating'` 时调用返回 409。

若已有旧测试断言「删除后出视频」，需改为新断言。运行：`uv run --project backend pytest backend/tests/integration/test_pipeline.py -k tail_frame`（按 memory 约定直接用 uv 跑测试，不走 podman）。

前端：现有 Playwright 测试若覆盖尾帧流程，按 CLAUDE.md mock AI 端点；delete-tail-frame 本身不触发 AI，mock 其返回 2xx 即可。本次不强制新增 e2e，除非已有尾帧 e2e 套件。

## 风险与回滚

- 行为变更：旧 `delete-tail-frame`「删除即出视频」语义被移除。实现前检索确认当前**无任何调用方**依赖旧语义（前端未接入，应无其他后端调用）。→ 低风险。
- 回滚：还原 `delete_tail_frame` 函数体 + 移除前端三处改动即可。

## 实现顺序（交给 writing-plans 细化）

1. 后端改造 `delete_tail_frame` + 文件删除。
2. 后端集成测试（TDD：先写断言新行为的测试）。
3. 前端 `api.ts` → `ShotsPage` → `ShotCard`（含二次确认）。
4. 手动验证：生成尾帧 → 删除（带确认）→ 确认回到无尾帧态、可重新生成、不自动出视频。

## 环境说明（重要）

尾帧**基础**功能（`tf_status` / `tf_confirmed` / `target_last_frame_path`、generate/confirm/extract 端点）已提交在 HEAD。但本功能依赖的以下 4 处改动目前在 `feat/kie-ai-video-provider` 的**未提交工作区改动**中：

1. `backend/app/api/pipeline.py` — `delete-tail-frame` 端点本体（待改造）+ `generate-tail-frame` 的 `skip_tail_frame=False` 重启逻辑。
2. `backend/app/models/project.py` — `skip_tail_frame` 列定义。
3. `backend/app/models/schemas.py` — Shot 序列化的 `skip_tail_frame: bool` 字段。
4. `backend/app/db.py` — `skip_tail_frame` 列的 `ALTER TABLE` 迁移。

此外 `backend/tests/integration/test_pipeline.py` 也有未提交改动（疑似 delete 端点旧测试），实现时需核对并改为新行为断言。

实现阶段必须在该主 checkout 上进行（git worktree 只从 commit 拉取，会丢失这些未提交改动）。本 spec 文档独立，故先在隔离 worktree 写就。
