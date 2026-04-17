# Video Maker Agent · Design Specification

**Date:** 2026-04-11
**Status:** Draft (awaiting user review)
**Author:** brainstormed with Claude Code

---

## Section 1 — 系统概览与目标

### 产品定位

一个内网团队共享的"一句话到成片"视频制作工具。用户在前端输入一句话的视频主题 + 上传若干参考图（人物 / 场景），后端自动完成 **剧本生成 → 分镜 → 运镜提示词 → Veo 3 视频 → 尾帧传递 → 成片合并** 的全流水线。团队成员可共享工作区，所有项目默认对所有人可见，无鉴权。

### 核心能力 (MVP)

| 能力 | 描述 |
|---|---|
| 一句话输入 | 用户输入主题文本 + 可选多图参考（角色图、场景图，各 1-3 张） |
| 分镜生成 | Screenwriter Agent (Gemini 2.5 Pro 多模态) 读图 + 读文，输出结构化 `storyboard.json`（含 `scene_overview` 和 `shots[]`） |
| 运镜提示词生成 | Director Agent (Gemini 2.5 Flash) 为每个 shot 生成中文 Veo 运镜提示词，末尾自动追加「角色说：『{text}』」 |
| 视频生成 | Video Generator 用 Veo 3 API。首帧图来源按每镜的"对齐属性"决定：对齐上一镜时用上一镜尾帧（ffmpeg 抽取），独立首帧时用角色参考图 |
| 分镜对齐控制 | 每镜有独立的 `align_with_previous` 布尔开关。口播一镜到底的内容默认全部对齐；切镜/蒙太奇/多角度切换类内容可关闭对齐，让每镜独立使用角色参考图作为首帧 |
| 审批卡点 | 两次：脚本审批 (`SCRIPT_REVIEW`)、所有分镜视频审批 (`SHOT_REVIEW`)，每次可修改 / 重跑单镜或重跑全部 |
| 成片导出 | 用户在最终页面点"导出"按钮，后端用 ffmpeg concat demuxer 零损耗拼接 |
| 多人共享 | 所有项目对所有成员可见；用户只填名字做标识，无鉴权 |
| 进度实时推送 | 后端通过 SSE 推送 pipeline 状态、每镜完成情况、错误 |

### 非目标 (MVP 明确不做)

- 背景音乐叠加
- 字幕烧录 / 导出 SRT
- 转场效果（直接 concat，shot 之间硬切）
- 用户注册 / 登录 / 密码
- 付费 / 配额 / 计费
- 自然语言聊天式修改
- 多 Provider LLM（仅 Gemini）
- 项目评论、协作、@ 提及

### 成功标准

- 从"一句话 + 3 张参考图"到"一个完整 mp4 成片"，用户操作不超过 6 次（创建 / 输入 / 审批脚本 / 审批视频 / 导出 / 下载）
- 单条 6 镜头视频的端到端时间 ≤ 15 分钟（瓶颈在 Veo 3 API）
- 任何一镜可在审批页面重跑，不影响其它镜头
- 上一镜尾帧到下一镜首帧的视觉衔接误差在目视可接受范围内

---

## Section 2 — 系统架构与组件

### 部署拓扑 (Docker Compose)

```
┌─────────────────────────────────────────────────────────┐
│                      Docker Compose                    │
│                                                         │
│  ┌──────────────┐   HTTP/SSE   ┌──────────────────┐     │
│  │  Frontend    │ ◄──────────► │   Backend API    │     │
│  │  (Next.js)   │              │   (FastAPI)      │     │
│  │  :3000       │              │   :8000          │     │
│  └──────────────┘              └────────┬─────────┘     │
│                                         │               │
│                                         │ enqueue       │
│                                         ▼               │
│                                ┌──────────────────┐     │
│                                │  Redis (arq)     │     │
│                                │  :6379           │     │
│                                └────────┬─────────┘     │
│                                         │ dequeue       │
│                                         ▼               │
│                                ┌──────────────────┐     │
│                                │  Worker          │     │
│                                │  (arq + Python)  │     │
│                                └────────┬─────────┘     │
│                                         │               │
│         ┌───────────────────────────────┼───────┐       │
│         ▼                               ▼       ▼       │
│  ┌─────────────┐              ┌─────────────┐  ┌─────┐  │
│  │ SQLite      │              │ storage/    │  │ ... │  │
│  │ metadata.db │              │ projects/   │  └─────┘  │
│  └─────────────┘              └─────────────┘           │
│                                                         │
└─────────────────────────────────────────────────────────┘
                         │
                         │ HTTPS
                         ▼
              ┌────────────────────┐
              │ Vertex AI          │
              │ - Gemini 2.5 Pro   │
              │ - Gemini 2.5 Flash │
              │ - Veo 3            │
              └────────────────────┘
```

### 4 个服务

| 服务 | 技术栈 | 职责 |
|---|---|---|
| `frontend` | Next.js 14 (App Router) + TypeScript + Tailwind + shadcn/ui | 项目列表、新建、wizard 分步审批、SSE 订阅 |
| `api` | FastAPI + SQLAlchemy + Pydantic | REST 接口、SSE 端点、文件上传、SQLite CRUD、任务入队 |
| `worker` | arq + google-genai SDK + ffmpeg-python | 执行 pipeline 各步骤、调用 Vertex AI、操作文件系统 |
| `redis` | 官方 redis:7-alpine | arq 任务队列 + pub/sub 事件广播 |

### 后端分层

```
backend/
├── app/
│   ├── main.py                  # FastAPI 入口
│   ├── config.py                # 环境变量（Vertex 凭证、路径、Redis）
│   ├── db.py                    # SQLite 连接、SQLAlchemy engine
│   ├── models/                  # SQLAlchemy ORM 模型
│   │   ├── project.py           # Project, Shot, Event, ReferenceImage
│   │   └── schemas.py           # Pydantic 请求/响应模型
│   ├── api/                     # FastAPI 路由
│   │   ├── projects.py          # 项目 CRUD
│   │   ├── pipeline.py          # 触发 pipeline 步骤、审批
│   │   ├── uploads.py           # 参考图上传
│   │   ├── assets.py            # 静态文件服务（图/视频下载）
│   │   └── stream.py            # SSE 进度端点
│   ├── services/
│   │   ├── state_machine.py     # 有限状态机定义与转换
│   │   ├── storage.py           # storage/ 目录布局、路径生成
│   │   └── events.py            # Redis pub/sub 事件广播（SSE 源）
│   └── agents/                  # 纯函数 Agent 实现
│       ├── llm.py               # GeminiProvider 薄抽象
│       ├── screenwriter.py      # Gemini Pro 多模态 → storyboard.json
│       ├── director.py          # Gemini Flash → motion_prompt.txt
│       ├── video_generator.py   # Veo 3 调用 + 轮询 + 下载 mp4
│       ├── frame_porter.py      # ffmpeg-python 抽尾帧
│       └── merger.py            # ffmpeg concat demuxer 合成成片
├── worker/
│   ├── arq_worker.py            # arq WorkerSettings
│   └── tasks.py                 # arq 任务函数（调用 agents + 更新状态机）
├── prompts/
│   ├── screenwriter.md          # 迁移自根目录
│   └── director.md              # 迁移自根目录
├── storage/                     # 运行时生成（挂载为 volume）
│   └── projects/{project_id}/...
├── metadata.db                  # SQLite 文件
├── requirements.txt
└── Dockerfile
```

`veo_director.md` 在 MVP 中废弃：其"图 + 分镜 → 视频"的语义由 `director.py`（生成 prompt）+ `video_generator.py`（调 Veo）组合实现。

### 前端分层

```
frontend/
├── app/
│   ├── page.tsx                 # 项目列表首页
│   ├── projects/
│   │   ├── new/page.tsx         # 创建项目（输入主题 + 上传图）
│   │   └── [id]/
│   │       ├── page.tsx         # wizard 主页（根据状态渲染不同阶段）
│   │       ├── script/page.tsx  # 脚本审批页面
│   │       ├── shots/page.tsx   # 分镜视频审批页面
│   │       └── export/page.tsx  # 导出页面
│   └── layout.tsx
├── components/
│   ├── UploadZone.tsx           # 参考图多文件拖拽上传
│   ├── ShotCard.tsx             # 单镜卡片（首帧 + 视频 + 编辑/重跑按钮）
│   ├── ProgressStream.tsx       # SSE 订阅 + 进度条
│   └── UserBadge.tsx            # localStorage 里的当前用户名
├── lib/
│   ├── api.ts                   # 后端 API 客户端封装
│   ├── sse.ts                   # EventSource 封装
│   └── state.ts                 # 简单状态（Zustand 或 React Context）
├── package.json
└── Dockerfile
```

### 组件职责一句话总结

- **FastAPI**：接请求、读写 SQLite、把耗时任务丢给 arq、用 SSE 把 Redis pub/sub 事件推给前端
- **Worker**：arq 任务执行器，调用 agents/ 里的纯函数，产物写 `storage/`，状态写 SQLite，事件发 Redis pub/sub
- **Agents**：5 个纯函数模块（Screenwriter / Director / VideoGenerator / FramePorter / Merger）+ 1 个 LLM provider 薄抽象
- **State Machine**：一个显式的 enum + 合法转换表，所有状态变更必须走这里，保证状态一致性

---

## Section 3 — 状态机与数据流

### 核心状态枚举

```python
class ProjectStatus(str, Enum):
    DRAFT              = "draft"               # 刚创建，尚未触发任何 pipeline
    SCRIPTING          = "scripting"           # Screenwriter agent 运行中
    SCRIPT_REVIEW      = "script_review"       # 脚本已生成，等待用户审批
    SHOT_GENERATING    = "shot_generating"     # 正在批量生成分镜视频
    SHOT_REVIEW        = "shot_review"         # 所有分镜视频已生成，等待用户审批
    EXPORTING          = "exporting"           # Merger 运行中
    EXPORTED           = "exported"            # 成片可下载
    FAILED             = "failed"              # 某一步不可恢复失败

class ShotStatus(str, Enum):
    PENDING            = "pending"
    PROMPT_GENERATING  = "prompt_generating"
    VIDEO_GENERATING   = "video_generating"
    COMPLETED          = "completed"
    FAILED             = "failed"
```

### 状态机转换图

```
DRAFT -> SCRIPTING                    (user submits)
SCRIPTING -> SCRIPT_REVIEW            (storyboard.json saved)
SCRIPTING -> FAILED                   (gemini error)

SCRIPT_REVIEW -> SCRIPTING            (user: regenerate)
SCRIPT_REVIEW -> SHOT_GENERATING      (user: approve)

SHOT_GENERATING -> SHOT_REVIEW        (all shots completed OR partial failure causing pause)
SHOT_GENERATING -> FAILED             (unrecoverable error)

SHOT_REVIEW -> SHOT_GENERATING        (user: regenerate selected shots)
SHOT_REVIEW -> SCRIPTING              (user: edit script; archives current storyboard, clears shots)
SHOT_REVIEW -> EXPORTING              (user: export)

EXPORTING -> EXPORTED                 (merged.mp4 saved)
EXPORTING -> FAILED                   (ffmpeg error)

EXPORTED -> EXPORTING                 (user: re-export)
EXPORTED -> SHOT_GENERATING           (user: edit shot)
EXPORTED -> SCRIPTING                 (user: edit script)

FAILED -> DRAFT                       (user: reset; only recovery path in MVP)
```

### 关键状态转换语义

1. **SCRIPT_REVIEW → SCRIPTING**：用户要求重新生成脚本。旧 `storyboard.json` 归档（带时间戳后缀），shots 表全部清空。
2. **SHOT_REVIEW → SHOT_GENERATING**：用户在审批页面选中了 N 个需要重跑的 shot。只有这几个 shot 的 `ShotStatus` 被重置为 `PENDING`，其它保持 `COMPLETED`。worker 只会处理 `PENDING` 的 shot。
3. **SHOT_REVIEW → SCRIPTING**（退回脚本）：罕见情况。所有 shot 清空，storyboard.json 归档。
4. **EXPORTED → SHOT_GENERATING / SCRIPTING**：允许用户在拿到成片后继续修改。
5. **FAILED → 恢复路径**：`FAILED` 状态只允许转到 `DRAFT`（`POST /reset`），等价于"放弃当前运行结果，回到初始状态重新开始"。如果失败的是单镜（Director / VideoGenerator / FramePorter），pipeline 会软失败回到 `SHOT_REVIEW` 而非 `FAILED`，所以 `ProjectStatus.FAILED` 只代表 Screenwriter 或 Merger 的不可恢复错误。**MVP 不支持"从 `FAILED` 退回上一个可恢复状态"的半恢复路径**，简化状态机。

### 分镜对齐 (align_with_previous) 语义

并非所有分镜都需要首尾帧对齐。典型区分：

| 内容类型 | 推荐对齐策略 |
|---|---|
| 数字人口播、一镜到底 | 全部 `align_with_previous = True`，用尾帧串起连续动作 |
| 多角度切镜 / 蒙太奇 / 场景切换 | 对应的镜头 `align_with_previous = False`，每镜用角色参考图作为独立起始帧 |
| 混合型（开场口播 + 中间切插画面） | 逐镜决定，灵活组合 |

**数据层**：每个 `shot` 有独立的 `align_with_previous: bool` 字段，默认 `True`。

**决策权**：
1. **Screenwriter 初步判定**：多模态 LLM 根据分镜的视觉连续性（是不是同一个动作的延续、视角是否发生明显切换）输出每镜的 `align_with_previous` 值。默认偏向 `True`，只有明确的切镜才设 `False`
2. **用户最终决定**：脚本审批页每个 shot 卡片带一个切换开关"🔗 与上一镜连续 / ✂ 独立首帧"，用户可以按意图覆盖 screenwriter 的判定

**首帧选择规则**（统一逻辑）：

```python
def pick_first_frame(project, shot):
    if shot.shot_id == 1 or not shot.align_with_previous:
        # 独立首帧：用第一张 character 参考图
        return first_character_reference(project)
    else:
        # 对齐上一镜：用上一镜落盘的尾帧
        prev = get_shot(project.id, shot.shot_id - 1)
        return prev.last_frame_path
```

注意：shot 1 永远没有上游可对齐，强制走角色参考图。

### 重跑与级联语义

重跑 shot N 的影响链取决于下游 shot 的对齐属性：

| 场景 | 下游影响 |
|---|---|
| shot N+1 的 `align_with_previous = False` | 下游不受影响（它本来就不用 N 的尾帧） |
| shot N+1 的 `align_with_previous = True` | 下游旧视频的首帧是 N 的**旧尾帧**，和 N 的**新尾帧**不一致，出现断层 |

**MVP 的选择是：尊重用户意图，不自动级联重跑**。理由：
- 用户可能故意只改当前镜，接受断层
- 需要级联时，用户在"重跑选中"里把 N..M 都勾上即可
- 自动级联会放大成本（Veo 3 调用昂贵），不符合 YAGNI

**UI 智能提示**：重跑按钮旁动态计算下游 `align_with_previous = True` 的连续镜范围，仅当存在这种情况时才显示提示："shot N+1 是与 shot N 连续的镜头，只重跑 N 可能导致衔接断层，建议同时勾选 shot N+1..M"。如果下游都是 `align_with_previous = False`，不显示任何警告。

### 状态机的并发规则

- **单项目串行**：同一个 `project_id` 在任意时刻只能有一个 worker task 在跑，由 SQLite 的 `project.status` 字段作为互斥锁（FastAPI 在触发前检查当前状态是否可转入目标状态）。
- **跨项目并行**：多个项目的 pipeline 可以并行在 arq worker 池里跑（默认池大小 4，可配）。
- **Shot 级串行**：同一项目内的多个 shot 必须**串行**执行，因为"第 N 镜的首帧 = 第 N-1 镜的尾帧"，存在强依赖。**不能并行生成 shots。**

### 数据流：一次完整的"一句话到成片"

```
[用户] → POST /api/projects {title, theme_text}
         Header: X-User-Name: wayne
         ← {project_id, status: "draft"}

[用户] → POST /api/projects/{id}/reference-images (multipart, kind=character|scene)
         ← {image_ids: [...]}

[用户] → POST /api/projects/{id}/start
         后端:
           1. 校验 status == DRAFT 且有 ≥1 张 character 参考图
           2. state_machine.transition(project, SCRIPTING)
           3. arq.enqueue("run_screenwriter", project_id)
           4. Redis PUBLISH events:{id} {type: "state_change", to: SCRIPTING}
         ← 202 Accepted

[前端] SSE /api/projects/{id}/stream 收到 state_change → 切到"脚本生成中"界面

[worker] run_screenwriter(project_id):
           1. 读 SQLite：project + reference_images
           2. 读 prompts/screenwriter.md + 拼接多模态 user message
           3. 调 Gemini 2.5 Pro（multimodal, JSON mode with Pydantic schema）
           4. 校验字数规则（超限 → 警告标记，不阻塞）
           5. storyboard.json 落盘 + scene_overview 写 projects 表 + shots 批量 insert
           6. state_machine.transition → SCRIPT_REVIEW
           7. Redis PUBLISH {type: "script_ready", storyboard}

[前端] 收到 script_ready → 渲染脚本审批页面

[用户] → POST /api/projects/{id}/approve-script
         → state_machine.transition → SHOT_GENERATING
         → arq.enqueue("run_shot_pipeline", project_id)

[worker] run_shot_pipeline(project_id):
           for shot in shots WHERE status = PENDING ORDER BY shot_id:
             a. shot.status = PROMPT_GENERATING
             b. Director agent → motion_prompt.txt 落盘 + 更新 shots.motion_prompt
             c. first_frame 决定:
                  if shot_id == 1: 第一张 character 参考图
                  else: shots/shot_{n-1}/last_frame.png
             d. shot.status = VIDEO_GENERATING
             e. Veo 3 generate_videos(prompt, image, spoken_text, duration)
             f. 轮询 operation（每 10s await asyncio.sleep），最多 5 分钟
             g. 下载 mp4 → shots/shot_{n}/output.mp4
             h. FramePorter → last_frame.png
             i. shot.status = COMPLETED
             j. Redis PUBLISH {type: "shot_completed", shot_id}
             -- 如果任何一步抛错，则将 shot.status = FAILED + 写 error_message
                并 break loop（剩下的 PENDING shots 保持 PENDING 不动）
           state_machine.transition → SHOT_REVIEW   -- 无论全部成功还是部分失败都转入 REVIEW
           Redis PUBLISH {type: "all_shots_ready", has_failures: bool}

[前端] 渲染 shot 审批页：每镜卡片含尾帧预览、视频播放器、编辑运镜提示词、重跑选择框

[用户] 可选动作:
  - 编辑某镜 motion_prompt → PATCH /api/projects/{id}/shots/{shot_id}
  - 重跑若干镜 → POST /api/projects/{id}/regenerate-shots {shot_ids}
  - 导出 → POST /api/projects/{id}/export

[worker] run_merger(project_id):
           1. 列出所有 COMPLETED shot 的 output.mp4 按 shot_id 排序
           2. 写 filelist.txt
           3. ffmpeg -f concat -safe 0 -i filelist.txt -c copy merged.mp4
           4. state_machine.transition → EXPORTED
           5. Redis PUBLISH {type: "export_done", download_url}
```

### SSE 事件类型

| 事件类型 | payload | 触发时机 |
|---|---|---|
| `state_snapshot` | `{status, shots, storyboard}` | SSE 连接建立时的初始快照 |
| `state_change` | `{from, to}` | 项目状态转换时 |
| `script_ready` | `{storyboard}` | Screenwriter 完成 |
| `shot_started` | `{shot_id}` | Director 开始处理某镜 |
| `shot_progress` | `{shot_id, sub_status}` | Shot 进入 VIDEO_GENERATING / 等 Veo |
| `shot_completed` | `{shot_id, preview_url, video_url}` | 单镜视频和尾帧都就绪 |
| `shot_failed` | `{shot_id, error}` | 单镜失败（pipeline 暂停） |
| `all_shots_ready` | `{}` | 所有 shot 完成，进入 SHOT_REVIEW |
| `export_done` | `{download_url}` | 成片就绪 |
| `pipeline_failed` | `{reason}` | 不可恢复错误 |

### 关键约束

- **Shot 级顺序强制**：`run_shot_pipeline` 在 worker 里按 `shot_id` 升序串行处理，**绝不并行**
- **幂等性**：`run_shot_pipeline` 只处理 `status = PENDING` 的 shot，所以"重跑某几镜"和"首次全跑"是同一段代码
- **状态变更原子化**：所有 SQLite 更新包一个事务；状态机转换走统一的 `state_machine.transition(project, target, actor)` 函数

---

## Section 4 — 数据模型与 API

### 4.1 SQLite 数据模型

#### `projects` 表

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | UUID v4 |
| `title` | TEXT NOT NULL | 项目名称（用户输入） |
| `theme_text` | TEXT NOT NULL | 一句话主题 |
| `creator_name` | TEXT NOT NULL | 创建者昵称 |
| `status` | TEXT NOT NULL | `ProjectStatus` 枚举字符串 |
| `scene_overview` | TEXT NULL | screenwriter 生成的全局场景描述 |
| `storyboard_path` | TEXT NULL | `storage/.../storyboard.json` 相对路径 |
| `final_video_path` | TEXT NULL | `storage/.../final/merged.mp4` 相对路径 |
| `error_message` | TEXT NULL | FAILED 状态时的错误详情 |
| `created_at` | DATETIME | |
| `updated_at` | DATETIME | |

索引：`(status)`, `(creator_name)`, `(created_at DESC)`

#### `reference_images` 表

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | UUID |
| `project_id` | TEXT FK → projects.id CASCADE | |
| `kind` | TEXT NOT NULL | `'character'` / `'scene'` |
| `filename` | TEXT NOT NULL | 原始文件名 |
| `storage_path` | TEXT NOT NULL | `storage/.../reference_images/xxx.png` 相对路径 |
| `order_index` | INTEGER NOT NULL | 同 kind 内的展示顺序 |
| `created_at` | DATETIME | |

索引：`(project_id, kind, order_index)`

#### `shots` 表

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `project_id` | TEXT FK → projects.id CASCADE | |
| `shot_id` | INTEGER NOT NULL | 分镜序号（从 1 开始） |
| `text` | TEXT NOT NULL | 台词 |
| `shot_type` | TEXT NOT NULL | 景别 |
| `visual_description` | TEXT NOT NULL | 动作与表情描述 |
| `shot_duration` | INTEGER NOT NULL | 4 / 6 / 8 秒 |
| `status` | TEXT NOT NULL | `ShotStatus` 枚举 |
| `align_with_previous` | BOOLEAN NOT NULL DEFAULT 1 | 是否与上一镜首尾帧对齐。shot_id=1 忽略该字段。False 时首帧用角色参考图 |
| `motion_prompt` | TEXT NULL | director 生成的运镜提示词 |
| `first_frame_path` | TEXT NULL | 首帧图相对路径（实际使用的那张：可能是角色参考图或上一镜尾帧） |
| `video_path` | TEXT NULL | `output.mp4` 相对路径 |
| `last_frame_path` | TEXT NULL | 尾帧图相对路径 |
| `veo_operation_id` | TEXT NULL | Veo 3 长任务 ID |
| `word_count_warning` | BOOLEAN DEFAULT 0 | 台词字数是否超出 `shot_duration` 规定范围 |
| `error_message` | TEXT NULL | 单镜失败时的错误 |
| `created_at` | DATETIME | |
| `updated_at` | DATETIME | |

索引：`(project_id, shot_id)` UNIQUE, `(project_id, status)`

#### `events` 表（审计）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `project_id` | TEXT FK | |
| `actor` | TEXT NOT NULL | `'user:{name}'` / `'system:worker'` |
| `event_type` | TEXT NOT NULL | `state_change` / `shot_completed` / `error` / ... |
| `payload` | TEXT (JSON) | 事件详情 |
| `created_at` | DATETIME | |

索引：`(project_id, created_at DESC)`

**单一事实源原则**：storyboard.json 整文件落盘作归档，同时 `scene_overview` 存进 `projects` 表、`shots[]` 展平存进 `shots` 表。SQLite 是运行时的单一事实源，文件作备份 / 人工检查。

### 4.2 REST API 设计

**约定**：所有请求/响应都是 JSON。错误格式统一：`{"error": {"code": "...", "message": "..."}}`。写操作必须携带 `X-User-Name` header。

#### 项目管理

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/api/projects` | 项目列表（`?status=`, `?creator=`, `?sort=created_at:desc`，分页 `?limit=20&offset=0`） |
| `POST` | `/api/projects` | 创建项目：body `{title, theme_text}` → 返回 `{project_id, status: "draft"}` |
| `GET` | `/api/projects/{id}` | 项目详情（含 shots、reference_images 列表、storyboard） |
| `DELETE` | `/api/projects/{id}` | 删除项目（级联删 DB 行 + `storage/projects/{id}/` 整个目录） |

#### 参考图上传

| Method | Path | 说明 |
|---|---|---|
| `POST` | `/api/projects/{id}/reference-images` | multipart，字段 `files[]` + `kind` (`character`/`scene`) |
| `DELETE` | `/api/projects/{id}/reference-images/{image_id}` | 删除单张参考图 |

#### Pipeline 触发与审批

| Method | Path | 说明 |
|---|---|---|
| `POST` | `/api/projects/{id}/start` | 从 `DRAFT` 启动：校验 ≥1 张 character 参考图，转入 `SCRIPTING` |
| `POST` | `/api/projects/{id}/regenerate-script` | 从 `SCRIPT_REVIEW` 重新生成脚本 |
| `PATCH` | `/api/projects/{id}/storyboard` | 在 `SCRIPT_REVIEW` 状态直接修改分镜内容 |
| `POST` | `/api/projects/{id}/approve-script` | 从 `SCRIPT_REVIEW` → `SHOT_GENERATING` |
| `POST` | `/api/projects/{id}/regenerate-shots` | body `{shot_ids: [2, 5]}` 重置并重跑 |
| `PATCH` | `/api/projects/{id}/shots/{shot_id}` | 在 `SHOT_REVIEW` 状态手动编辑 `motion_prompt`（不自动重跑） |
| `POST` | `/api/projects/{id}/export` | 从 `SHOT_REVIEW` → `EXPORTING` |
| `POST` | `/api/projects/{id}/reset-to-script` | 从 `SHOT_REVIEW` → `SCRIPTING`，归档当前 storyboard、清空 shots |
| `POST` | `/api/projects/{id}/reset` | 从 `FAILED` → `DRAFT`，清空 shots、归档 storyboard、保留参考图（用户重新触发 start） |

#### 资源下载

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/api/projects/{id}/assets/{kind}/{file}` | 静态代理，`kind` ∈ `reference_images` / `shots/shot_N` / `final` |
| `GET` | `/api/projects/{id}/final.mp4` | 直接下载成片 |

#### 实时进度 (SSE)

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/api/projects/{id}/stream` | `text/event-stream`，首发 `state_snapshot`，之后增量推送 |

### 4.3 状态机转换校验

所有状态变更走 `state_machine.transition(project, target, actor)`：
1. 查表验证 `(current_status, target_status)` 是否合法
2. 在 SQLite 事务里更新 `projects.status` 和 `updated_at`
3. 写一行 `events` 审计记录
4. 向 Redis 发 `state_change` 事件

非法转换抛 `InvalidTransitionError`，API 返回 409 Conflict。

### 4.4 用户身份处理

无鉴权。前端 localStorage 存 `user_name`，每次写请求用 `X-User-Name: wayne` 发送。后端从 header 取值写入 `creator_name`（创建）或 `events.actor`（审计）。不做任何验证。

---

## Section 5 — Agent 实现细节与外部集成

### 5.1 共享 LLM Provider

一个极薄的 provider 抽象位于 `backend/app/agents/llm.py`：

```python
# 接口示意
class GeminiProvider:
    def __init__(self, project, location, credentials):
        self.client = genai.Client(vertexai=True, project=..., location=...)

    async def generate_json(self, model: str, system: str, user_parts: list, schema: dict) -> dict:
        """用于 screenwriter：多模态输入 + 结构化 JSON 输出"""

    async def generate_text(self, model: str, system: str, user: str) -> str:
        """用于 director：纯文本"""
```

- 底层用 `google-genai` SDK（Vertex AI 模式）
- JSON mode 通过 `response_mime_type="application/json"` + `response_schema=...` 强制
- screenwriter 和 director 都依赖这个类

配置项：

```env
GEMINI_SCRIPT_MODEL=gemini-2.5-pro
GEMINI_DIRECTOR_MODEL=gemini-2.5-flash
```

### 5.2 Screenwriter Agent

**输入：** `project_id`（从 SQLite 读 `theme_text` + `reference_images`）

**流程：**
1. 加载 `prompts/screenwriter.md` 作为 system prompt
2. 构造多模态 user message parts：
   - 每张 character 参考图（按 `order_index`） + 文字 `"角色参考图 {i}"`
   - 每张 scene 参考图 + 文字 `"场景参考图 {i}"`
   - 文字 `"主题：{theme_text}"`
3. 调用 `generate_json(model=gemini-2.5-pro, schema=StoryboardSchema)`：

   ```python
   class ShotItem(BaseModel):
       shot_id: int
       text: str
       shot_type: Literal["Close-up", "Medium Shot", "Wide Shot"]
       visual_description: str
       shot_duration: Literal[4, 6, 8]
       align_with_previous: bool = True  # 默认与上一镜连续；切镜/蒙太奇时设 False

   class Storyboard(BaseModel):
       scene_overview: str
       shots: list[ShotItem]
   ```

4. 字数校验（screenwriter.md Rule 3）：4s→15-18、6s→22-25、8s→30-34。超限的 shot 在 DB 里 `word_count_warning = 1`，不阻塞流水线
5. 写 `storage/.../storyboard.json`（归档）
6. 在 SQLite 事务里更新 `projects.scene_overview`、批量 insert `shots`、`projects.status → SCRIPT_REVIEW`

**Prompt 更新要求**：`prompts/screenwriter.md` 需要新增一条规则，明确告诉 LLM："为每个 shot 输出 `align_with_previous`：如果本镜与上一镜是同一个动作/视角/场景的延续（例如口播中间不切镜），设为 `true`；如果本镜是切到新角度、新场景或新动作，设为 `false`。shot 1 始终设为 `false`（视情况也可以是 true，后端统一忽略）"。

### 5.3 Director Agent

**输入：** 单行 `shot` 记录

**流程：**
1. 加载 `prompts/director.md`
2. 把 4 个槽位（`shot_id`, `shot_type`, `visual_description`, `text`）塞进 user prompt
3. 调 `generate_text(model=gemini-2.5-flash)` 得到中文运镜提示词
4. **强制后处理**：如果 `text` 非空，在运镜提示词末尾追加：

   ```
   角色说：『{text}』
   ```

   即便 LLM 漏了，后处理保底。
5. 写 `shots.motion_prompt` + `storage/.../shots/shot_{n}/motion_prompt.txt`

### 5.4 VideoGenerator Agent

**输入：** `shot.motion_prompt` + `first_frame_image_path`

**首帧图选择逻辑**（由 worker 的 `run_shot_pipeline` 统一决策，传入 VideoGenerator）：

```python
def pick_first_frame(project, shot):
    if shot.shot_id == 1 or not shot.align_with_previous:
        # 独立首帧：shot 1 或明确标记"不对齐"的切镜
        return first_character_reference(project)
    else:
        # 对齐上一镜尾帧
        prev = get_shot(project.id, shot.shot_id - 1)
        return prev.last_frame_path
```

解析后，worker 把实际使用的首帧路径写入 `shots.first_frame_path`，方便审批页展示以及排错。

**Veo 3 调用**（示意代码，实际字段名以 google-genai SDK 当前版本为准）：

```python
client = genai.Client(vertexai=True, ...)

operation = client.models.generate_videos(
    model="veo-3.0-generate-001",
    prompt=motion_prompt,
    image=types.Image.from_file(first_frame_path),
    config=types.GenerateVideosConfig(
        aspect_ratio="16:9",
        duration_seconds=shot.shot_duration,
        generate_audio=True,
        spoken_text=shot.text,
        number_of_videos=1,
    ),
)

while not operation.done:
    await asyncio.sleep(10)
    operation = client.operations.get(operation)

video_bytes = operation.response.generated_videos[0].video.video_bytes
Path(output_mp4_path).write_bytes(video_bytes)
```

**注意：**
- 台词字段以"专用字段传入而非拼 prompt"为设计目标；若 SDK 当前版本的字段名不是 `spoken_text`，以 SDK 文档为准，implementation plan 阶段需验证
- 轮询间隔 10 秒，**最大等待 5 分钟**
- 超时或 API 错误 → `ShotStatus.FAILED`
- `veo_operation.json` 存 operation 对象做调试用

### 5.5 FramePorter Agent

```python
import ffmpeg

def extract_last_frame(video_path: str, output_path: str):
    (
        ffmpeg
        .input(video_path, sseof=-0.1)
        .output(output_path, vframes=1, **{'q:v': 2})
        .overwrite_output()
        .run(quiet=True)
    )
```

落盘后立即写 `shots.last_frame_path`。

### 5.6 Merger Agent

```python
def merge_shots(shot_paths: list[str], output_path: str):
    filelist = Path(tempfile.mktemp(suffix=".txt"))
    filelist.write_text("\n".join(f"file '{p}'" for p in shot_paths))

    (
        ffmpeg
        .input(str(filelist), format="concat", safe=0)
        .output(output_path, c="copy")
        .overwrite_output()
        .run(quiet=True)
    )
```

### 5.7 错误与重试策略

| Agent | 失败类型 | 处理 |
|---|---|---|
| Screenwriter | Gemini API 瞬时错误 (5xx/timeout) | 自动重试 2 次（指数退避） → `FAILED` |
| Screenwriter | JSON 校验失败 | 重试 1 次带"请严格遵守格式"提示 → `FAILED` |
| Director | Gemini API 瞬时错误 | 自动重试 2 次 → `ShotStatus.FAILED`，pipeline 暂停 |
| VideoGenerator | Veo API 非 200 | 重试 1 次 → `ShotStatus.FAILED`，pipeline 暂停 |
| VideoGenerator | 轮询超时 (>5 分钟) | `ShotStatus.FAILED`，不重试 |
| FramePorter | ffmpeg 非 0 退出 | 不重试，`ShotStatus.FAILED` |
| Merger | ffmpeg 非 0 退出 | 不重试，`ProjectStatus.FAILED` |

**软失败策略**：单镜失败 (`ShotStatus.FAILED`) 不会把整个 `ProjectStatus` 标为 FAILED。pipeline 遇到后停止继续处理，把 `ProjectStatus` 设回 `SHOT_REVIEW`，让用户看到哪些镜失败并选择"只重跑失败的镜"。全局失败（Screenwriter / Merger）才进入 `ProjectStatus.FAILED`，需用户显式重试。

### 5.8 外部依赖

```
# backend/requirements.txt 节选
fastapi>=0.110
uvicorn[standard]>=0.27
sqlalchemy>=2.0
aiosqlite>=0.19
arq>=0.25
redis>=5.0
pydantic>=2.6
pydantic-settings>=2.2
google-genai>=0.3
ffmpeg-python>=0.2
python-multipart>=0.0.9
sse-starlette>=2.0
python-json-logger>=2.0
```

系统依赖：`ffmpeg`（Dockerfile 里 `apt-get install`）。

---

## Section 6 — 前端 UX 流程

### 6.1 页面结构

```
/                              首页 / 项目列表
/projects/new                  新建项目
/projects/{id}                 项目详情入口（根据状态自动跳转）
/projects/{id}/script          脚本审批页
/projects/{id}/shots           分镜视频审批页
/projects/{id}/export          导出页
```

**路由策略**：`/projects/{id}` 是智能入口，读状态后跳转：
- `DRAFT` → 停在 `/projects/{id}`，显示"开始生成"按钮
- `SCRIPTING` / `SCRIPT_REVIEW` → 跳 `/script`
- `SHOT_GENERATING` / `SHOT_REVIEW` → 跳 `/shots`
- `EXPORTING` / `EXPORTED` → 跳 `/export`
- `FAILED` → 停在 `/projects/{id}`，显示错误和"重试"按钮

### 6.2 首页 / 项目列表

- 顶部：搜索框（title 模糊匹配） + 筛选（status、creator_name） + 排序
- 右上角：`UserBadge`（显示并可修改当前用户名，存 localStorage） + "新建项目" 按钮
- 主体：项目卡片网格，展示标题、创建者、创建时间、状态徽章、最终视频缩略图（若 EXPORTED）、进度条（若进行中）
- 每卡右上小菜单：打开 / 删除
- **实时更新策略 (MVP)**：每 5 秒轮询一次列表。未来可升级为聚合 SSE 频道

### 6.3 新建项目页

表单字段：
- 项目标题
- 主题（一句话）
- 角色参考图（必填，1-3 张，拖拽上传）
- 场景参考图（可选，1-3 张）

点"创建并开始"后：
1. `POST /api/projects`
2. `POST /api/projects/{id}/reference-images` × N
3. `POST /api/projects/{id}/start`
4. 跳转到 `/projects/{id}/script`

### 6.4 脚本审批页

**SCRIPTING 状态**：加载动画 + "生成脚本中..."，SSE 收到 `script_ready` 自动切换。

**SCRIPT_REVIEW 状态**：
- 顶部：`scene_overview` 可编辑文本区
- 主体：分镜卡片列表，每卡展示 `shot_id / shot_type / shot_duration / text / visual_description`
- 字数超限的 shot 显示黄色警告徽章（"台词 19 字（建议 15-18）"），**不阻止通过**
- 每卡顶部有一个对齐开关：**🔗 与上一镜连续**（默认，由 screenwriter 判定）或 **✂ 独立首帧**。shot 1 的开关固定为"独立首帧"不可修改。用户点击即可覆盖 screenwriter 的判定
- 每卡有 [编辑] 按钮，打开 modal 改 `text / shot_type / visual_description / shot_duration / align_with_previous`，保存走 `PATCH /storyboard`
- 底部按钮：[重新生成脚本] [通过，开始生成视频 →]

### 6.5 分镜视频审批页

**SHOT_GENERATING 状态**：
- 总体进度条（N/M 完成）+ 预计剩余时间
- 每镜卡片显示子状态：`pending / prompt_generating / video_generating / completed / failed`
- SSE 事件驱动卡片状态实时更新

**SHOT_REVIEW 状态**：
- 每镜卡片：
  - 视频播放器
  - 右上角对齐标签：🔗（与上一镜连续）或 ✂（独立首帧）
  - 尾帧预览缩略图（供下一镜用，如果下游有对齐镜）
  - 运镜提示词（可编辑）
  - [编辑提示词] [查看首帧] [下载] 按钮
  - 多选框"选中重跑"
- 失败的 shot 红框，顶部横幅提示"N 个镜头失败"
- 底部按钮：[退回修改脚本] [重跑选中的镜] [全部通过，导出 →]
- 有失败 shot 时"全部通过，导出"按钮**禁用**
- 编辑 `motion_prompt` 后不自动重跑，需要用户主动勾选并点"重跑"
- **智能断层提示**：前端在用户勾选重跑集合 `S` 时动态计算："对每个 n ∈ S，找出最大的连续下游 [n+1..m] 使得 shot_{n+1..m}.align_with_previous 全为 true"；如果这些下游镜未被一并勾选，显示"shot N 的下游 [N+1..M] 是连续镜头，只重跑 N 可能导致衔接断层，建议同时勾选它们 [一键追加]"。如果下游都是独立首帧镜，不显示任何警告

### 6.6 导出页

**EXPORTING 状态**：进度动画 + "正在合成最终视频..."

**EXPORTED 状态**：
- 视频播放器展示最终成片
- 元信息：时长、分镜数、分辨率
- [下载 MP4] 主按钮 + [返回项目列表]
- [退回到分镜审批] [退回到脚本审批] 次级入口

### 6.7 错误与加载状态

- SSE 连接断开：EventSource 自动重连，重连时拉一次项目详情同步状态
- 网络错误：右上角红色 Toast，3 秒自动消失
- 长时间无进度：SHOT_GENERATING 下若 60 秒无 SSE 事件，显示"检查服务器状态..."并主动拉一次项目详情

### 6.8 UI 风格

- **技术栈**：Next.js 14 + Tailwind CSS + shadcn/ui
- **调性**：冷色调（蓝灰主色），信息密度偏高（生产力工具）
- **视频预览**：原生 `<video controls>`
- **图片预览**：shadcn Dialog lightbox

---

## Section 7 — 测试、部署与可观测性

### 7.1 测试策略

| 层级 | 框架 | 覆盖范围 |
|---|---|---|
| 单元测试 | `pytest` + `pytest-asyncio` | agents/、state_machine、Pydantic schemas |
| 集成测试 | `pytest` + `httpx.AsyncClient` + fakeredis | FastAPI 路由、SQLite 读写、SSE 事件流 |
| Agent mock 测试 | `pytest` + 人工 mock | 所有 Gemini / Veo 3 调用走 mock |
| 端到端烟雾测试 | `pytest`，开关 `E2E=1` | 真实 API，1 镜头最短链路 |
| 前端组件测试 | Vitest + React Testing Library | ShotCard、ProgressStream |
| 前端 E2E | Playwright（MVP 后） | 完整 wizard 流程 |

**关键 mock**：
- `FakeLLMProvider`：根据 prompt hash 返回预录响应
- `FakeVideoGenerator`：返回固定的 1 秒测试 mp4 + last_frame.png
- ffmpeg 真实调用（本地工具）

**CI 门槛**：
- 后端：单元 + 集成测试通过，覆盖率 ≥ 70%
- 前端：单元 + tsc 通过
- E2E：不跑在 CI，本地手动触发

### 7.2 部署

**docker-compose.yml**：

```yaml
services:
  frontend:
    build: ./frontend
    ports:
      - "3000:3000"
    environment:
      - NEXT_PUBLIC_API_BASE=http://localhost:8000
    depends_on:
      - api

  api:
    build: ./backend
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
    ports:
      - "8000:8000"
    environment:
      - GOOGLE_APPLICATION_CREDENTIALS=/secrets/vertex.json
      - GCP_PROJECT=${GCP_PROJECT}
      - GCP_LOCATION=${GCP_LOCATION}
      - REDIS_URL=redis://redis:6379
      - STORAGE_ROOT=/app/storage
      - DATABASE_URL=sqlite+aiosqlite:////app/metadata.db
    volumes:
      - ./storage:/app/storage
      - ./metadata.db:/app/metadata.db
      - ./secrets/vertex.json:/secrets/vertex.json:ro
    depends_on:
      - redis

  worker:
    build: ./backend
    command: arq worker.arq_worker.WorkerSettings
    environment:
      # same as api
    volumes:
      # same as api
    depends_on:
      - redis

  redis:
    image: redis:7-alpine
    volumes:
      - redis_data:/data

volumes:
  redis_data:
```

**凭证管理**：
- Vertex AI service account key 放 `./secrets/vertex.json`，`.gitignore` 排除
- `.env.example` 提交进仓库，列出必需变量；`.env` 不提交

**启动流程**：
1. `cp .env.example .env` 填入 `GCP_PROJECT` / `GCP_LOCATION`
2. `cp ~/vertex-sa.json ./secrets/vertex.json`
3. `docker compose up -d`
4. 访问 `http://<server-ip>:3000`

**初始化**：SQLite 首次启动由 SQLAlchemy `create_all()` 自动建表。未来数据模型变动引入 Alembic。

**资源要求**：4 核 8GB 内网机器；100 MB / 项目的存储估算；Veo 调用走外网，无 GPU 需求。

### 7.3 可观测性

**日志**：
- 后端统一用 Python `logging` + `python-json-logger` JSON 格式
- 关键字段：`timestamp, level, logger, project_id, shot_id, actor, event, message`
- 必记事件：状态转换、Agent 调用开始/结束、Gemini/Veo 返回耗时、错误堆栈
- 输出到 stdout/stderr，由 `docker compose logs` 查看

**健康检查**：
- `GET /api/health` → `{status, redis, db}`
- `GET /api/version` → `{commit}`（构建时注入）

**审计**：
- `events` 表作为完整项目操作日志
- 内部页面 `/projects/{id}/audit` 展示 events

**错误追踪**：MVP 不接 Sentry，通过日志 + `error_message` 字段排查。

### 7.4 数据备份

`scripts/backup.sh`：手动或 cron 执行

```bash
#!/bin/bash
tar czf /var/backups/video_maker/backup-$(date +%Y%m%d).tar.gz storage/ metadata.db
```

MVP 不做增量备份，不做自动清理。

### 7.5 安全

无鉴权模型下的底线：
- **API key 严格不上传**：`.gitignore` 排除 `.env` / `secrets/`
- **文件上传校验**：`image/png|jpeg|webp`，单文件 ≤ 10 MB，每项目总数 ≤ 6 张
- **路径穿越防护**：`storage/` 路径构造必须走 `storage.py` helper，UUID 校验 `project_id`
- **CORS**：API 只允许 `http://localhost:3000` 和内网域名（配置项）
- **SSRF 防护**：无 URL 参数入口，所有图片走 multipart

### 7.6 仓库结构

```
video_maker/
├── README.md              # 项目简介、架构图、快速开始、troubleshooting
├── docker-compose.yml
├── .env.example
├── .gitignore
├── backend/
├── frontend/
├── docs/
│   └── superpowers/
│       └── specs/
│           └── 2026-04-11-video-maker-agent-design.md  ← 本文档
├── scripts/
│   └── backup.sh
└── storage/               # .gitignore 排除，运行时生成
```

根目录的 `screenwriter.md` / `director.md` / `veo_director.md` 作为原始草稿保留；`backend/prompts/` 持有运行时副本。`veo_director.md` 在 MVP 中不被代码使用，仅作设计参考。

---

## 附录 A：设计决策摘要

| 决策点 | 选项 | 理由 |
|---|---|---|
| 视频生成后端 | Vertex AI Veo 3 | 已有凭证，原生音频 + 口型同步 |
| 交互模型 | 严格阶梯式 wizard（审批 2 次） | 状态机简单，每阶段批量处理 |
| 多图参考 | character + scene 两类 | 角色一致性 + 场景氛围 |
| 首帧优先级 | 由 `align_with_previous` 决定：对齐→上一镜尾帧；独立→角色参考图；shot 1 恒为角色参考图 | 只有口播一镜到底需要对齐，切镜/蒙太奇无需 |
| 对齐决策权 | Screenwriter 初判 + 用户在脚本审批页覆盖 | LLM 推荐，用户兜底 |
| 重跑级联策略 | 不自动级联，UI 智能提示下游连续镜 | 成本控制 + 尊重用户意图 |
| Director 粒度 | 一步（prompt 生成内嵌 Veo 调用） | 减少 wizard 停顿次数 |
| 部署形态 | 内网多人共享 | Docker Compose，4 服务 |
| 用户模型 | 全局共享 + 无鉴权 | 最低复杂度，适合内网信任 |
| LLM 选型 | Gemini 2.5 Pro (screenwriter) + Flash (director) | 共享 Vertex 凭证 |
| 音频 | Veo 3 原生，TTS 未来扩展 | 一次出口型+声音，避免对齐问题 |
| 后台任务 | arq + Redis | 轻量、asyncio 风格 |
| 前后端通信 | SSE 单向推送 | 比 WebSocket 简单 |
| 尾帧提取 | 视频生成后立即 | 磁盘成本可忽略，便于审批展示 |
| ffmpeg 调用 | ffmpeg-python | 依赖明确 |
| 元数据存储 | SQLite | 零运维 + SQL 查询能力 |
| 合并时机 | 用户手动"导出" | 保留回旋余地 |
| 合并方式 | concat demuxer，无重编码 | 零损耗、极快 |
| BGM / 字幕 | MVP 不做 | YAGNI |
| Agent 编排 | 纯状态机 + 薄 Agent 封装 | 贴合纯函数提示词，零框架依赖 |
| 首页实时更新 | MVP 轮询 | 简单直接 |
| 前端组件库 | shadcn/ui | 代码直抄，无运行时依赖 |
| 字数超限 | UI 标黄警告，不重试 | 严格重试会显著拖慢 |
| Veo 轮询超时 | 5 分钟 | 覆盖正常 30s-2min 范围 |
| 单镜失败策略 | 软失败，pipeline 暂停回 SHOT_REVIEW | 用户只重跑失败镜 |
| 测试覆盖率 | 后端 ≥ 70% | 合理门槛 |
| 备份 | 手动 tar 脚本 | MVP 级 |
