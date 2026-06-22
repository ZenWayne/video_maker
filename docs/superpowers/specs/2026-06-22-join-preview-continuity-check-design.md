# 临时 Join Shot 连贯性检测 — 设计文档

**日期**: 2026-06-22
**状态**: 已批准，待实现

## 背景与目标

在 shot 审阅阶段，用户需要快速检查相邻分镜之间的**连贯性**（画面衔接、是否穿帮、动作是否流畅）。当前只能逐个 shot 单独播放，或走完整 export 流程把**全部** completed shot 合成 `final/merged.mp4` 才能看到拼接效果——后者太重，且会污染正式产物。

本功能提供一个**临时/调试性质**的工具：把用户当前选中的若干 shot 临时拼接成一条视频，在页面内弹窗播放，专用于肉眼检测连贯性。它不进入正式 export 流程，不写 DB，不修改任何 shot 素材。

## 设计决策（已与用户确认）

| 决策点 | 选择 |
|--------|------|
| 拼接哪些 shot | 当前选中的 shots（复用已有 `selectedShotIds` 多选），按 `shot_id` 升序拼接 |
| 拼接方式 | 纯拼接（无转场），复用 `merge_shots()` |
| 查看方式 | 页面内 modal `<video>` 播放器 |
| 执行方式 | **同步**（不走 worker 队列） |
| 临时文件 | **单一固定文件**，每次覆盖 |

## 架构

### 后端

#### 1. Storage helper

文件：`backend/app/services/storage.py`

新增：
```python
def join_preview_path(project_id: str) -> str:
    """临时连贯性预览视频的固定输出路径（每次覆盖）。"""
    # -> storage_root/projects/{project_id}/previews/join_preview.mp4
```
- 复用现有的 `projects/{project_id}/` 根目录约定。
- 需确保 `previews/` 目录存在（参考同文件其它 helper 的 mkdir 模式）。

#### 2. 新端点

文件：`backend/app/api/pipeline.py`（pipeline router）

```
POST /api/projects/{project_id}/join-preview
Header: X-User-Name (复用现有 _require_user 依赖)
body:   { "shot_ids": [3, 4, 5] }
200:    { "preview_url": "/api/media/projects/{pid}/previews/join_preview.mp4?t=<frame_count_or_size>" }
```

行为：
1. 校验 `shot_ids` 长度 ≥ 2，否则返回 400。
2. 按 `shot_ids` 查询对应 Shot；每个必须属于该 project、`status == COMPLETED`、且 `video_path` 文件存在，否则 400（提示哪个 shot 不满足）。
3. **按传入顺序**取 `video_path` 列表（前端负责传升序 id）。
4. 调用 `merge_shots(shot_paths, join_preview_path(project_id))`（concat demuxer，无转场，无重编码）。
5. 通过 `to_media_url()` 生成 URL，**追加 cache-busting 查询参数**（用输出文件大小或当前时间戳），返回 `preview_url`。

要点：
- 同步执行，直接在请求处理中跑完 ffmpeg。纯 `c=copy` 拼接，几个 shot 为秒级，无需异步轮询。
- 输出固定到 `previews/join_preview.mp4`，每次覆盖，不累积、无需清理。
- **只读** `shot.video_path`，输出到独立 `previews/` 目录，不修改/重命名/删除任何 shot 素材 → 不触发 CLAUDE.md 的"素材文件变更审计"。

### 前端

#### 1. API client

文件：`frontend-vite/src/lib/api.ts`

```typescript
joinPreview(projectId: string, shotIds: number[]): Promise<{ preview_url: string }>
```
POST 到 `/api/projects/{projectId}/join-preview`，body `{ shot_ids: shotIds }`。

#### 2. ShotsPage

文件：`frontend-vite/src/pages/ShotsPage.tsx`

- 在已有的批量操作按钮区（选中 shots 时出现的那排）新增 **"连贯性预览"** 按钮。
- 启用条件：`selectedShotIds.size >= 2`（不足 2 个时禁用并提示）。
- 点击：
  1. 取 `[...selectedShotIds].sort((a,b)=>a-b)`。
  2. 调 `api.joinPreview(projectId, sortedIds)`。
  3. 拿到 `preview_url` → 打开 modal 播放。
  4. loading / 错误用现有的提示机制（toast / 状态）。
- modal：轻量内联组件即可，含 `<video controls autoPlay>` 指向 `preview_url`，关闭时停止播放。无需新建复杂文件。

## 数据流

```
用户勾选 shot 3,4,5
  → 点击"连贯性预览"
  → POST /join-preview { shot_ids:[3,4,5] }
  → 后端校验 + merge_shots([v3,v4,v5], previews/join_preview.mp4)
  → 返回 /api/media/.../join_preview.mp4?t=...
  → 前端 modal <video> 播放
```

## 错误处理

| 情况 | 处理 |
|------|------|
| 选中 < 2 个 shot | 前端禁用按钮；后端 400 兜底 |
| 某 shot 非 COMPLETED / 无 video_path | 后端 400，消息指明 shot_id |
| ffmpeg 拼接失败 | 后端 500 + 错误信息；前端弹错误提示 |
| 浏览器缓存旧预览 | URL 加 cache-busting 查询参数 |

## 测试

- 后端：`uv run pytest`（直接运行，不走 podman）。覆盖：
  - 正常：2+ 个 COMPLETED shot → 200 + 文件生成。
  - 边界：< 2 个 shot → 400。
  - 边界：含未完成 / 缺失 video_path 的 shot → 400。
  - ffmpeg 调用 mock 或用 fixture 小视频（避免真实重编码开销；merge 本身用 `c=copy`，可用极短 fixture）。
- 前端：若有 Playwright，mock `POST /join-preview`（避免触发真实 ffmpeg），验证按钮启用条件与 modal 出现。

## 明确不做（YAGNI）

- crossfade 选项
- 手动输入区间
- cascade 连贯性警告联动
- 写入 DB / 记录预览历史
- 异步任务 / 进度条
- 预览文件的定时清理（单一覆盖式文件，天然无需）
