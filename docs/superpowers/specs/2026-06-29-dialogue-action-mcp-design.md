# 设计: 台词与动作生成 MCP 服务

> 状态: 设计评审 / 待用户确认
> 日期: 2026-06-29
> 范围: 新增 `backend/mcp_server/` 包 + 新增后端接口 `PUT /api/projects/{id}/storyboard` + `deploy/docker-compose.dev.yml` 新增 `mcp` 服务

## 背景与目标

video_maker 当前的内容生成链路是:

```
theme + 角色参考图
  → Screenwriter (Gemini)  生成 storyboard {scene_overview, shots[]}，每个 shot 含 text(台词) + visual_description
  → Director (Gemini)       逐 shot 生成 motion_prompt(动作)
  → Veo 3                   渲染视频
```

目标是新增一个 **MCP 服务**，让一个外部 LLM agent（如 Claude）能够:

1. 读取项目 / 镜头上下文（主题、角色、场景概述、相邻镜头、字数约束、创作规范）。
2. **由调用方 agent 自己撰写**台词（`text`）与动作（`motion_prompt`）。
3. 通过 MCP 把这些内容**提交并修改**回 video_maker。
4. 进一步可以**整体改写分镜脚本**（提交完整 `{scene_overview, shots[]}`，可增删镜头）。

MCP 本身**不调用任何 LLM**——生成在调用方 agent 处发生，MCP 只是一个「智能桥接层」，封装后端 HTTP API 并提供校验与上下文。

## 关键设计决策（已与用户确认）

| # | 决策 | 选择 |
|---|------|------|
| 1 | 生成发生在哪 | **调用方 agent 撰写**；MCP 为桥接层，后端零 LLM 成本 |
| 2 | 写能力范围 | 编辑台词 + 动作；**不触发渲染/再生成**（无 Veo 计费、无素材文件副作用） |
| 3 | 传输方式 | **远程 HTTP/SSE** MCP 服务 |
| 4 | MCP 如何访问后端 | 走后端现有 **HTTP API**（非直连 DB），保留状态机与事件 |
| 5 | 鉴权与定位 | **无鉴权（可信网络）**；工具以 `project_id` 为参数 + `list_projects` 发现 |
| 6 | 分镜脚本范围 | **可整体改写 storyboard**：新增全量替换接口（可增删镜头），保留逐 shot 编辑 |
| 7 | 部署形态 | **方案 A**：复用 backend 镜像，作为 compose 中独立 `mcp` 服务 |

## 架构

### 服务形态

- 新增 compose 服务 `mcp`，由 **backend 同一镜像**构建（与 `worker` 同模式），以不同命令启动 FastMCP 的 streamable HTTP/SSE 服务。
- `mcp` 通过 compose 内网 DNS 以 `httpx` 调用 `backend` 服务（`BACKEND_BASE_URL=http://backend:8000`）。
- 无鉴权，仅暴露在可信网络。MCP 不需要任何 secret（无鉴权、无 LLM key）。

### 代码布局

放在 `backend/` 内，以便直接复用后端 Pydantic schema 与校验函数，同时作为独立进程/容器运行:

```
backend/
  mcp_server/
    __init__.py
    server.py        # FastMCP app + 工具定义（薄）
    client.py        # 封装后端 HTTP API 的 async httpx 客户端
    context.py       # 为 agent 整形 project/shot 上下文
    validation.py    # 复用 app.agents.screenwriter.validate_word_count + WORD_COUNT_RULES
    guidelines.py    # 精炼后的台词/动作创作规范（distill 自 screenwriter.md / director.md）
  pyproject.toml     # + fastmcp 依赖
```

**放在 `backend/` 内的理由**: 可直接 `import app.models.schemas`（`ShotResponse`/`ShotUpdate`/`ShotItem`）与 `app.agents.screenwriter.validate_word_count`，避免重复定义；同时以独立进程运行，可单独重启。

### 配置 / 接线

- 新增环境变量 `BACKEND_BASE_URL=http://backend:8000`，`MCP_PORT`（如 `8765`）。
- compose `mcp` 服务: 同 backend 镜像，`command: uv run python -m mcp_server.server`，对外发布端口，`depends_on: backend`。
- Makefile: `make dev` 一并拉起；可选 `make dev-mcp` 单独运行。
- `fastmcp` 加入 `backend/pyproject.toml`，`uv sync --project backend`。

## 数据契约（JSON 形状）

项目中存在两种相关 JSON，明确区分:

### 1. Storyboard / 分镜脚本 JSON（Screenwriter 产物，存为 `storyboard.json`）

形状 `{scene_overview, shots[]}`，其中每个 shot 为 `ShotItem`（见 `app/models/schemas.py`）:

```jsonc
{
  "scene_overview": "...",
  "shots": [
    {
      "shot_id": 1,
      "text": "<台词 / dialogue>",
      "shot_type": "Close-up | Medium Shot | Wide Shot",
      "visual_description": "...",
      "shot_duration": 4,            // 4 | 6 | 8
      "align_with_previous": true,
      "reference_image_hint": null   // 可选
    }
  ]
}
```

**注意: storyboard JSON 不含 `motion_prompt`**——动作/motion 是后续由 Director 逐 shot 生成，仅存于 Shot 行，不入 `storyboard.json`。

### 2. 编辑 JSON（写工具发送的 `ShotUpdate` → `PATCH /shots/{id}`）

按决策 #2，MCP 的逐 shot 写入只发送:

```jsonc
{ "text": "<台词>", "motion_prompt": "<动作 / motion>" }
```

读工具返回完整 `ShotResponse`（含 `text`、`motion_prompt`、`status`、`word_count_warning`、各类路径等）。

## 后端改动: 新增 storyboard 全量替换接口

现有 `PATCH /api/projects/{id}/storyboard`（`StoryboardUpdate`）**只能按 `shot_id` 更新已存在的 shot**，无法增删镜头，且不重写 `storyboard.json`。为支持「整体改写分镜」需新增接口。

### 接口

```
PUT /api/projects/{project_id}/storyboard
```

**Body**（新增专用请求模型 `StoryboardReplace`——区别于 `StoryboardUpdate` 的两字段皆 `Optional`，此处 `scene_overview` 与 `shots` 均**必填**表示全量替换）:

```jsonc
{
  "scene_overview": "...",
  "shots": [ /* ShotItem[]，见上 */ ]
}
```

### 行为（全量替换）

1. **状态校验**: `project.status` 必须为 `SCRIPT_REVIEW`，否则 `409`（与现有 `PATCH /storyboard` 一致，保证始终处于「渲染前」，无已生成素材文件）。
2. **按 `shot_id` upsert**: 已存在则更新字段；payload 中新出现的 `shot_id` 则创建；DB 中存在但 payload 缺失的 shot 则删除。
3. **重写 `storyboard.json`**: 使文件与 DB 一致（修复现有 `PATCH` 的文件漂移问题）。
4. **更新 `project.scene_overview`**。
5. 返回 `ProjectResponse`。

### 输入校验

- `shot_id` 在 payload 内唯一。
- `shot_type` ∈ {`Close-up`, `Medium Shot`, `Wide Shot`}。
- `shot_duration` ∈ {4, 6, 8}。
- `shots` 非空。

### Shot 素材文件变更审计（遵守 CLAUDE.md）

本接口受「Shot 素材文件变更审计」约束。审计结论:

- 接口限定 `SCRIPT_REVIEW` 状态——此时尚未进入 `SHOT_GENERATING`，正常流程下 shot 无 `output.mp4`/`last_frame.png` 等素材文件，故删除/替换 shot **正常无素材文件需清理**。
- **防御性处理**: 删除某 shot 时，若其 output 目录确实存在（异常/历史数据），一并删除该目录，避免遗留过期文件。
- 不涉及 `vc_status`/`cc_status`/尾帧备份（这些只在渲染后产生），无需重置。
- 下游读取方仍通过 `shot.video_path` / `shot_output_path()` 取路径，不受影响。

## MCP 工具清单

### 读 / 上下文

| 工具 | 参数 | 返回 |
|------|------|------|
| `list_projects` | — | `[{id, title, status, shot_count, aspect_ratio}]`（`GET /api/projects`） |
| `get_project` | `project_id` | `{theme, status, aspect_ratio, scene_overview, characters:[{filename,kind}], shot_count}`（`GET /api/projects/{id}`）。注: 项目无独立 `language` 字段，语言隐含于 `theme`/现有台词 |
| `list_shots` | `project_id` | 每 shot: `{shot_id, order_index, shot_type, shot_duration, align_with_previous, text, motion_prompt, visual_description, word_count, word_count_target, has_video}` |
| `get_shot` | `project_id, shot_id` | 完整 shot + **前后镜头台词片段**（连续性上下文）+ `word_count_target` + `has_video` |
| `get_authoring_guidelines` | — | 精炼创作规范: 字数表、台词须匹配现有台词/主题的语言、motion_prompt = 英文 + talking-head/唇形(lip-sync)约定（distill 自 `screenwriter.md`/`director.md`） |

### 写

| 工具 | 参数 | 行为 |
|------|------|------|
| `replace_storyboard` | `project_id, scene_overview, shots[]` | 封装 `PUT /storyboard`（结构 + 台词；要求 `SCRIPT_REVIEW`；可增删镜头） |
| `update_dialogue` | `project_id, shot_id, text` | 校验后 `PATCH /shots` 写 `text`，返回更新后的 shot + 校验报告 |
| `update_motion` | `project_id, shot_id, motion_prompt, sync_lip_marker=true` | `PATCH /shots` 写 `motion_prompt`；若 `sync_lip_marker` 且该 shot 有台词，镜像 Director 的唇形标记约定，保证直接编辑与流水线产物一致 |
| `batch_update_shots` | `project_id, updates:[{shot_id, text?, motion_prompt?}]` | 单次调用批量 `PATCH /shots`（撰写整段分镜的台词/动作）；逐项结果，允许部分成功 |

合计 **5 读 + 4 写**。`batch_update_shots` 存在的理由: 「agent 一次性撰写整段分镜再提交」是主用例，一次往返优于 N 次。

### 两阶段创作流

```
replace_storyboard  → 设定脚本结构 + 台词（动作留空）
batch_update_shots  → 填充 motion_prompt（动作）
```

将 motion 保留在 Director 的概念层，与 storyboard JSON 分离。

## 校验规则

- **字数 = 建议性，不阻断**。复用 `screenwriter.validate_word_count()`，结果返回 `{actual, target_range, within_range: bool}`。仅**空白 `text`** 为硬性拒绝。理由: 字数规则面向英文词数（CJK 由 validator 内部按字符处理），硬阻断会造成误失败；返回目标值让撰写方 agent 自我修正即可。
- **`has_video` 提示**: 若 shot 已有渲染视频，写工具返回提示「已保存；在重新生成该镜头前不会改变现有视频」（按决策 #2 不触发再生成）。

## 数据流

```
agent → get_project / list_shots / get_authoring_guidelines      （收集上下文）
agent → 本地撰写台词 + 动作（或整段 storyboard）
agent → replace_storyboard / batch_update_shots / update_dialogue / update_motion
MCP   → httpx PUT/PATCH 后端 API（后端为唯一事实源）
后端  → 持久化，照常发出事件；UI 实时反映
MCP   → 返回更新后的 shot + 校验报告给 agent
```

## 错误处理

- **后端不可达 / 5xx** → 工具返回清晰 `error`（含 status），无半写（每个 PATCH 后端侧原子）。
- **404 project/shot** → 映射为干净的「not found」工具错误。
- **`409`（storyboard 非 SCRIPT_REVIEW）** → 明确提示需先进入 script review。
- **`batch_update_shots`** → 逐项成功/失败；单个坏 `shot_id` 不致整批失败。
- **校验错误** → 结构化返回，绝不静默放过。

## 测试策略

遵守 CLAUDE.md（真实服务、仅 mock 付费 LLM 调用——而本 MCP 不发起任何 LLM 调用），用 `uv run pytest` 直接运行:

- **单元**:
  - `validation.py` 字数边界用例。
  - `client.py` 请求整形（mock httpx）。
  - 唇形标记(lip-sync marker)逻辑。
- **后端接口**: `PUT /storyboard` 全量替换的集成测试——upsert / 新增 / 删除 / 非 `SCRIPT_REVIEW` 返回 409 / `storyboard.json` 与 DB 一致 / 删除 shot 的防御性目录清理。
- **MCP 集成**: FastMCP 内存客户端 → 工具 → **真实测试后端**（SQLite 测试库），断言 `PATCH`/`PUT` 已持久化到 shot。无需 mock（不涉及 LLM）。

## 实施任务概览（详细计划由 writing-plans 产出）

1. 后端: 新增 `PUT /projects/{id}/storyboard` 全量替换接口 + 请求模型 + 校验 + 素材审计防御清理。
2. 后端: 该接口集成测试。
3. `backend/pyproject.toml` 增加 `fastmcp`，`uv sync`。
4. `backend/mcp_server/`: `client.py`（httpx 封装）、`validation.py`、`context.py`、`guidelines.py`、`server.py`（工具定义）。
5. MCP 单元测试 + FastMCP 内存客户端集成测试。
6. `deploy/docker-compose.dev.yml`: 新增 `mcp` 服务（同 backend 镜像、不同命令、`depends_on: backend`、发布端口、`BACKEND_BASE_URL`）。
7. Makefile: `make dev` 纳入 `mcp`，新增 `make dev-mcp`。
8. 文档: 在后端文档中补充新接口与 MCP 工具说明。

## 风险 / 注意

- **状态机约束**: `replace_storyboard` 仅在 `SCRIPT_REVIEW` 可用；agent 须先确保项目已由 Screenwriter 跑到 script review（本设计不支持从空项目从零撰写——对应被否决的「从零创作」选项）。
- **`storyboard.json` 漂移**: 现有 `PATCH /storyboard` 不重写文件，新 `PUT` 必须重写以保持一致；二者并存时需注意行为差异（或后续将 `PATCH` 也对齐为重写）。
- **字数规则语言差异**: 英文词数规则对 CJK 仅作字符近似，故采用建议性校验，避免误阻断。
- **唇形标记一致性**: 直接 `PATCH motion_prompt` 绕过了 Director 的后处理；`update_motion` 的 `sync_lip_marker` 用于补齐，需与 `director.py` 现有逻辑保持同步，避免重复或缺失标记。
