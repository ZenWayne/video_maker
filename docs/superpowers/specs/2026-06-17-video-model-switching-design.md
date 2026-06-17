# 视频模型切换功能 — 设计文档

**日期**: 2026-06-17
**分支**: feat/kie-ai-video-provider
**状态**: 设计已确认，待写实现计划

## 目标

让用户为每个镜头（shot）选择视频生成**模型**：默认 `veo`，可选 `seeddance2.0`。
模型**厂商**（kie / vertex）由系统自动决定，对用户不可见；厂商默认改为 `kie`。

两条轴：
- **模型**（用户可选，按镜头粒度）：`veo`（默认）| `seeddance2.0`
- **厂商**（系统自动，用户不选）：默认 `kie`

## 核心决策（已与用户确认）

1. **切换粒度**：按镜头（per-shot）。同时提供**项目级默认**，新镜头继承项目默认，镜头可单独覆盖。
2. **厂商×模型组织**：用户只选模型，厂商自动定。
3. **可用时机**：任何时候都能改镜头模型，影响**下次生成**（已生成的镜头改模型后重生才生效）。
4. **厂商解析（方案 A）**：保留现有全局 `video_provider` 开关作为 veo 的厂商来源；其默认值从 `vertex` 翻成 `kie`。`seeddance2.0` 永远走 kie。保留 Vertex Veo 作为运维可全局切换的退路。
5. **seedance 配音**：`generate_audio` 默认开启（`True`），实现后用真实接口实测验证（见 Open Risks）。

## kie.ai seedance-2 接口规格（已查证）

- **model id**：`bytedance/seedance-2-fast`（做成可配置；文档另有 `bytedance/seedance-2`）
- **创建任务**：`POST /api/v1/jobs/createTask`
  ```json
  {
    "model": "bytedance/seedance-2-fast",
    "input": {
      "prompt": "...",
      "first_frame_url": "...",
      "last_frame_url": "...",
      "reference_image_urls": ["..."],
      "resolution": "1080p",
      "aspect_ratio": "16:9",
      "duration": 8,
      "generate_audio": true
    }
  }
  ```
  返回 `{ "code": 200, "data": { "taskId": "..." } }`
- **轮询**：`GET /api/v1/jobs/recordInfo?taskId=<id>`
  - `data.state`：`waiting` | `queuing` | `generating` | `success` | `fail`
  - 成功：`data.resultJson` 是 JSON 字符串，解析得 `{"resultUrls":["<mp4 url>"]}`
  - 失败：`data.failMsg` / `data.failCode`
- **约束**：
  - `duration`：4–15s（默认 5）。与 veo 的 (4,6,8) 不同。
  - `resolution`：480p / 720p / 1080p（默认 720p）。
  - `aspect_ratio`：1:1 / 4:3 / 3:4 / 16:9 / 9:16 / 21:9 / adaptive（默认 16:9）。
  - 首帧 / 尾帧 / 参考图均支持；**三种输入互斥**（不能同时用参考图与首尾帧）——正好命中现有 `_resolve_mode` 的优先级逻辑。

## 架构

### 厂商解析逻辑（方案 A）

```
get_video_provider(model):
    if model == "seeddance2.0":
        return KieSeedanceProvider()              # 恒 kie
    # model == "veo"（或未知回退）
    provider = settings.video_provider            # 默认 "kie"，可全局切 "vertex"
    return _VEO_PROVIDERS[provider]()             # KieVeoProvider | VertexVeoProvider
```

`generate_video(..., model=...)` 新增 `model` 参数，由 worker 传入 `shot.video_model`（回退 `project.default_video_model`）。

### Provider 类结构

抽出共享基类 `_KieBase`（封装 `_headers()`、`_upload_image()`、crop 输入），现有 `KieVeoProvider` 改为继承它；新增 `KieSeedanceProvider(_KieBase)`：

- `_resolve_inputs()`：复用现有 `_resolve_mode` 的优先级（参考图 > 首尾帧 > 纯文本），映射到 seedance 的 `reference_image_urls` / `first_frame_url`+`last_frame_url`。满足互斥约束。
- `_clamp_seedance_duration(s)` = `max(4, min(15, s))`。
- `_create_task()`：`POST /api/v1/jobs/createTask`，body 为 `{model, input:{...}}`。
- `_poll_result()`：`GET /api/v1/jobs/recordInfo`，按 `state` 判定；成功解析 `resultJson.resultUrls[0]`。
- `generate_video()`：上传图片（复用基类）→ createTask → 轮询 → 下载 MP4 bytes。

输出仍写 `output.mp4`，素材文件命名 / 备份逻辑零改动 → **不触发** CLAUDE.md 的「Shot 素材文件变更审计」。

## 改动清单（按文件）

### 后端

**`backend/app/models/project.py`**
- 新增 `class VideoModel(str, Enum): VEO = "veo"; SEEDDANCE_2 = "seeddance2.0"`
- `Project.default_video_model = Column(String(20), nullable=False, default="veo")`
- `Shot.video_model = Column(String(20), nullable=False, default="veo")`

**`backend/app/db.py`** — `_run_migrations()` 加两条幂等迁移（沿用 `aspect_ratio` 写法）：
```python
if not await _has_column("projects", "default_video_model"):
    await conn.execute(sa.text(
        "ALTER TABLE projects ADD COLUMN default_video_model VARCHAR(20) NOT NULL DEFAULT 'veo'"))
if not await _has_column("shots", "video_model"):
    await conn.execute(sa.text(
        "ALTER TABLE shots ADD COLUMN video_model VARCHAR(20) NOT NULL DEFAULT 'veo'"))
```

**`backend/app/config.py`**
- `video_provider` 默认 `"vertex"` → **`"kie"`**
- 新增 `kie_seedance_model: str = "bytedance/seedance-2-fast"`
- 新增 `kie_seedance_generate_audio: bool = True`
- 复用现有 `kie_api_key` / `kie_resolution` / `kie_poll_interval_seconds` / `kie_max_wait_seconds`，**无需新 secret**

**`backend/app/agents/video_generator.py`**
- 抽 `_KieBase`；`KieVeoProvider` 继承之
- 新增 `KieSeedanceProvider`
- `get_video_provider(model)` + `generate_video(..., model=...)` 按方案 A 解析
- 保留对旧调用的向后兼容（`model` 缺省 `"veo"`）

**`backend/app/models/schemas.py`**
- `ShotResponse` 加 `video_model: str = "veo"`
- `ProjectResponse` 加 `default_video_model: str = "veo"`
- `ShotUpdate` 加 `video_model: Optional[str]`（pattern 校验 `veo|seeddance2.0`）

**`backend/app/api/projects.py`**
- `ProjectCreate` 加 `default_video_model: str = Field(default="veo", pattern="^(veo|seeddance2\\.0)$")`
- `create_project` 写入 `default_video_model=body.default_video_model`
- shot 创建处（脚本生成分镜）令 `video_model = project.default_video_model` 继承
- shot 更新端点允许更新 `video_model`

**`backend/worker/tasks.py`** (~344)
- `generate_video(..., model=shot.video_model or project.default_video_model)`

### 前端（frontend-vite）

**`src/lib/types.ts`**
- `Shot` 加 `video_model: string`
- `Project` 加 `default_video_model: string`

**`src/pages/NewProjectPage.tsx`**
- 在画面比例下方加一组模型按钮（veo / seeddance2.0），镜像 `aspectRatio` 写法；提交时带上 `default_video_model`

**`src/components/ShotCard.tsx`**
- 编辑弹窗内、时长选择器旁加模型下拉（veo / seeddance2.0），保存走 ShotUpdate

## 测试（httpx 全 mock，沿用 `test_video_generator.py` 现有风格）

- `test_seedance_create_task_payload`：createTask 命中 `/api/v1/jobs/createTask`，body 为 `{model, input:{...}}`，model id 正确，input 嵌套正确
- `test_seedance_poll_success`：recordInfo `state=success`，正确解析 `resultJson.resultUrls[0]`
- `test_seedance_poll_fail`：`state=fail` 抛 `VideoGenerationError`（含 failMsg）
- `test_seedance_duration_clamp`：4–15 边界（如 3→4、20→15、8→8）
- `test_resolve_provider_veo_follows_setting`：`model="veo"` + `video_provider=kie/vertex` → 对应 provider
- `test_resolve_provider_seedance_always_kie`：`model="seeddance2.0"` 恒 KieSeedanceProvider
- `test_seedance_mutually_exclusive_inputs`：有参考图时只发 `reference_image_urls`，不发首尾帧
- `test_default_provider_is_now_kie`：更新现有 `test_default_provider_is_vertex`

测试遵循项目规则：所有 AI/模型调用 mock，不产生计费（见 CLAUDE.md Playwright mocking 规则；后端用 httpx mock）。

## Open Risks（实现后须验证）

1. **seedance 配音 vs veo3 台词生成**：现状 veo3 会按 prompt 里的台词生成配音，下游 VC（voice clone）再换音色。seedance 的 `generate_audio` 是否同样"念出台词"未知——若只出环境音/BGM 而不念台词，下游配音/对齐链路可能对不上。
   - **缓解**：`generate_audio` 默认 `True` 且可配置；实现后用真实接口跑一个含台词的镜头，验证产出音频是否含台词。若不含，需评估 VC 链路是否仍可用，或对 seedance 镜头改走纯 TTS 路径。
2. **分辨率/宽高比映射**：项目只用 16:9 / 9:16，seedance 支持更多但我们只透传这两种；非这两种时的行为不在本期范围。
3. **轮询超时**：seedance 时长可达 15s，生成耗时可能高于 veo；复用 `kie_max_wait_seconds`（当前 600s），实测若不够再调。

## 不做（YAGNI）

- 不在前端暴露厂商（kie/vertex）选择——厂商始终自动定。
- 不做 seedance 的 reference_video_urls / reference_audio_urls / web_search / nsfw_checker。
- 不支持非 16:9 / 9:16 的宽高比。
- 不在本期做 Vertex 上的 seedance（vertex 无 seedance）。
