# 后端架构文档

**项目：** Video Maker Agent
**版本：** MVP
**日期：** 2026-04-11
**参考规格：** `docs/superpowers/specs/2026-04-11-video-maker-agent-design.md`

---

## 1. 技术栈总览

| 层级 | 技术 | 说明 |
|---|---|---|
| Web 框架 | FastAPI | 异步 REST + SSE |
| 数据库 | SQLite + SQLAlchemy 2.0 (async) | 元数据持久化，aiosqlite 驱动 |
| 任务队列 | arq | 基于 Redis 的异步任务队列 |
| 消息总线 | Redis pub/sub | SSE 事件广播 |
| LLM | google-genai SDK (Vertex AI) | Gemini 2.5 Pro / Flash |
| 视频生成 | Veo 3 (google-genai SDK) | Vertex AI |
| 视频处理 | ffmpeg-python | 尾帧抽取 + 成片合并 |
| Python 包管理 | **uv** | 所有 Python 依赖安装、虚拟环境、脚本运行 |

---

## 2. 包管理：uv

所有涉及 Python 的操作统一使用 **uv**，不使用 pip / venv / poetry。

### 2.1 项目初始化

```bash
# 在 backend/ 目录下初始化
cd backend
uv init --no-workspace
```

这会生成 `pyproject.toml`（取代 `requirements.txt`）和 `.python-version`。

### 2.2 依赖声明（pyproject.toml）

```toml
[project]
name = "video-maker-backend"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.27",
    "sqlalchemy>=2.0",
    "aiosqlite>=0.19",
    "arq>=0.25",
    "redis>=5.0",
    "pydantic>=2.6",
    "pydantic-settings>=2.2",
    "google-genai>=0.3",
    "ffmpeg-python>=0.2",
    "python-multipart>=0.0.9",
    "sse-starlette>=2.0",
    "python-json-logger>=2.0",
]

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "httpx>=0.27",
    "fakeredis>=2.20",
    "coverage>=7.0",
]
```

### 2.3 常用命令

```bash
# 安装所有依赖（含 dev）
uv sync --group dev

# 安装生产依赖（不含 dev）
uv sync --no-dev

# 添加依赖
uv add fastapi

# 运行 FastAPI 开发服务器
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 运行 arq worker
uv run arq worker.arq_worker.WorkerSettings

# 运行测试
uv run pytest

# 运行单个测试文件
uv run pytest tests/test_state_machine.py -v
```

### 2.4 Dockerfile 中使用 uv

```dockerfile
FROM python:3.12-slim

# 安装 ffmpeg 系统依赖
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# 安装 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# 先复制依赖声明文件（利用 Docker 层缓存）
COPY pyproject.toml uv.lock ./

# 安装生产依赖，不创建虚拟环境（容器内直接用系统 Python）
RUN uv sync --no-dev --system

COPY . .
```

- `api` 容器启动命令：`uv run uvicorn app.main:app --host 0.0.0.0 --port 8000`
- `worker` 容器启动命令：`uv run arq worker.arq_worker.WorkerSettings`

---

## 3. 目录结构

```
backend/
├── pyproject.toml               # uv 依赖声明（取代 requirements.txt）
├── uv.lock                      # 锁文件（提交到 git）
├── .python-version              # 固定 Python 版本（如 3.12）
├── Dockerfile
├── app/
│   ├── main.py                  # FastAPI 入口，挂载所有路由
│   ├── config.py                # pydantic-settings 读取环境变量
│   ├── db.py                    # SQLAlchemy async engine + session factory
│   ├── models/
│   │   ├── project.py           # ORM：Project, Shot, Event, ReferenceImage
│   │   └── schemas.py           # Pydantic 请求/响应模型
│   ├── api/
│   │   ├── projects.py          # 项目 CRUD 路由
│   │   ├── pipeline.py          # pipeline 触发 / 审批路由
│   │   ├── uploads.py           # 参考图上传路由
│   │   ├── assets.py            # 静态文件代理（图/视频）
│   │   └── stream.py            # SSE 端点
│   ├── services/
│   │   ├── state_machine.py     # 有限状态机：枚举 + 合法转换表 + transition()
│   │   ├── storage.py           # storage/ 目录布局，路径生成函数
│   │   └── events.py            # Redis pub/sub 封装（publish / subscribe）
│   └── agents/
│       ├── llm.py               # GeminiProvider 薄抽象
│       ├── screenwriter.py      # Gemini 2.5 Pro 多模态 → storyboard.json
│       ├── director.py          # Gemini 2.5 Flash → motion_prompt
│       ├── video_generator.py   # Veo 3 调用 + 轮询 + 下载
│       ├── frame_porter.py      # ffmpeg 抽取尾帧
│       └── merger.py            # ffmpeg concat 合成成片
├── worker/
│   ├── arq_worker.py            # arq WorkerSettings
│   └── tasks.py                 # arq 任务函数（run_screenwriter / run_shot_pipeline / run_merger）
├── prompts/
│   ├── screenwriter.md          # Screenwriter 系统提示词
│   └── director.md              # Director 系统提示词
├── tests/
│   ├── unit/
│   │   ├── test_state_machine.py
│   │   ├── test_agents.py       # 所有 agent 都 mock LLM / Veo
│   │   └── test_schemas.py
│   └── integration/
│       ├── test_projects_api.py
│       ├── test_pipeline_api.py
│       └── test_sse.py
├── storage/                     # 运行时产物（挂载为 Docker volume，不提交 git）
│   └── projects/{project_id}/
│       ├── reference_images/
│       ├── storyboard.json
│       ├── shots/
│       │   └── shot_{n}/
│       │       ├── motion_prompt.txt
│       │       ├── first_frame.png
│       │       ├── output.mp4
│       │       └── last_frame.png
│       └── final/
│           └── merged.mp4
└── metadata.db                  # SQLite 文件（挂载为 Docker volume）
```

---

## 4. 服务架构

### 4.1 部署拓扑（Docker Compose）

```
┌─────────────────────────────────────────────────────────┐
│                      Docker Compose                     │
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
│  │ SQLite      │              │ storage/    │  │Redis│  │
│  │ metadata.db │              │ projects/   │  │ pub │  │
│  └─────────────┘              └─────────────┘  └─────┘  │
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

### 4.2 四个服务

| 服务 | 镜像来源 | 职责 |
|---|---|---|
| `frontend` | `./frontend` (Next.js) | 页面渲染、SSE 订阅、API 调用 |
| `api` | `./backend` | REST + SSE 端点、SQLite CRUD、任务入队 |
| `worker` | `./backend`（同镜像，不同启动命令） | 执行 pipeline 各阶段、调 Vertex AI、写文件系统 |
| `redis` | `redis:7-alpine` | arq 任务队列 + pub/sub 事件总线 |

`api` 和 `worker` 共用同一个 Docker 镜像，通过不同的启动命令区分角色。

---

## 5. 核心模块设计

### 5.1 配置（app/config.py）

使用 `pydantic-settings` 从环境变量读取配置，所有敏感值不硬编码：

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    gcp_project: str
    gcp_location: str = "us-central1"
    google_application_credentials: str
    redis_url: str = "redis://redis:6379"
    storage_root: str = "/app/storage"
    database_url: str = "sqlite+aiosqlite:////app/metadata.db"
    gemini_script_model: str = "gemini-2.5-pro"
    gemini_director_model: str = "gemini-2.5-flash"
    worker_pool_size: int = 4
    veo_poll_interval_seconds: int = 10
    veo_max_wait_seconds: int = 300

settings = Settings()
```

环境变量（docker-compose 注入）：

```env
GOOGLE_APPLICATION_CREDENTIALS=/secrets/vertex.json
GCP_PROJECT=my-gcp-project
GCP_LOCATION=us-central1
REDIS_URL=redis://redis:6379
STORAGE_ROOT=/app/storage
DATABASE_URL=sqlite+aiosqlite:////app/metadata.db
```

### 5.2 数据库（app/db.py）

```python
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

engine = create_async_engine(settings.database_url, echo=False)
AsyncSession = async_sessionmaker(engine, expire_on_commit=False)

async def get_session():
    async with AsyncSession() as session:
        yield session
```

所有路由通过 FastAPI 依赖注入获取 session，不直接使用全局 session。

### 5.3 状态机（app/services/state_machine.py）

状态枚举和合法转换表集中在此模块，所有状态变更必须调用 `transition()`：

```python
from enum import Enum

class ProjectStatus(str, Enum):
    DRAFT           = "draft"
    SCRIPTING       = "scripting"
    SCRIPT_REVIEW   = "script_review"
    SHOT_GENERATING = "shot_generating"
    SHOT_REVIEW     = "shot_review"
    EXPORTING       = "exporting"
    EXPORTED        = "exported"
    FAILED          = "failed"

class ShotStatus(str, Enum):
    PENDING           = "pending"
    PROMPT_GENERATING = "prompt_generating"
    VIDEO_GENERATING  = "video_generating"
    COMPLETED         = "completed"
    FAILED            = "failed"

VALID_TRANSITIONS: dict[ProjectStatus, set[ProjectStatus]] = {
    ProjectStatus.DRAFT:           {ProjectStatus.SCRIPTING},
    ProjectStatus.SCRIPTING:       {ProjectStatus.SCRIPT_REVIEW, ProjectStatus.FAILED},
    ProjectStatus.SCRIPT_REVIEW:   {ProjectStatus.SCRIPTING, ProjectStatus.SHOT_GENERATING},
    ProjectStatus.SHOT_GENERATING: {ProjectStatus.SHOT_REVIEW, ProjectStatus.FAILED},
    ProjectStatus.SHOT_REVIEW:     {ProjectStatus.SHOT_GENERATING, ProjectStatus.SCRIPTING,
                                    ProjectStatus.EXPORTING},
    ProjectStatus.EXPORTING:       {ProjectStatus.EXPORTED, ProjectStatus.FAILED},
    ProjectStatus.EXPORTED:        {ProjectStatus.EXPORTING, ProjectStatus.SHOT_GENERATING,
                                    ProjectStatus.SCRIPTING},
    ProjectStatus.FAILED:          {ProjectStatus.DRAFT},
}

class InvalidTransitionError(Exception):
    pass

async def transition(project, target: ProjectStatus, actor: str, session) -> None:
    """
    校验 → 更新 SQLite → 写审计 events → 发 Redis 事件。
    非法转换抛 InvalidTransitionError（API 层捕获返回 409）。
    """
```

**设计约束：**
- 非法转换返回 `409 Conflict`，不静默忽略
- 所有 SQLite 更新包在同一事务中
- 状态变更后立即向 Redis 发 `state_change` 事件

### 5.4 存储路径（app/services/storage.py）

所有文件路径通过此模块生成，避免散落在各处的字符串拼接：

```python
from pathlib import Path
from app.config import settings

def project_dir(project_id: str) -> Path:
    return Path(settings.storage_root) / "projects" / project_id

def reference_images_dir(project_id: str) -> Path:
    return project_dir(project_id) / "reference_images"

def shot_dir(project_id: str, shot_id: int) -> Path:
    return project_dir(project_id) / "shots" / f"shot_{shot_id}"

def storyboard_path(project_id: str) -> Path:
    return project_dir(project_id) / "storyboard.json"

def final_video_path(project_id: str) -> Path:
    return project_dir(project_id) / "final" / "merged.mp4"

def archived_storyboard_path(project_id: str, timestamp: str) -> Path:
    return project_dir(project_id) / f"storyboard_{timestamp}.json"
```

### 5.5 Redis 事件（app/services/events.py）

```python
import redis.asyncio as aioredis
import json

async def publish(redis_client, project_id: str, event: dict) -> None:
    channel = f"events:{project_id}"
    await redis_client.publish(channel, json.dumps(event))

async def subscribe(redis_client, project_id: str):
    """返回 async generator，SSE 端点使用"""
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(f"events:{project_id}")
    async for message in pubsub.listen():
        if message["type"] == "message":
            yield json.loads(message["data"])
```

### 5.6 Agent 设计（app/agents/）

所有 agent 都是**纯函数**（输入参数 → 返回结果 / 抛异常），不持有状态，便于单元测试和 mock。

#### LLM Provider（llm.py）

```python
from google import genai
from google.genai import types

class GeminiProvider:
    def __init__(self, project: str, location: str, credentials_path: str):
        self.client = genai.Client(vertexai=True, project=project, location=location)

    async def generate_json(
        self,
        model: str,
        system_prompt: str,
        user_parts: list,
        response_schema: type,
    ) -> dict:
        """多模态输入 + JSON 结构化输出（用于 Screenwriter）"""

    async def generate_text(
        self,
        model: str,
        system_prompt: str,
        user_message: str,
    ) -> str:
        """纯文本输出（用于 Director）"""
```

#### Screenwriter（screenwriter.py）

输入：`project`（含 `theme_text`）、`reference_images` 列表、`GeminiProvider`
输出：`Storyboard` Pydantic 对象

流程：
1. 读 `prompts/screenwriter.md` 作 system prompt
2. 构造多模态 user message（参考图 bytes + 文字标注）
3. 调 `generate_json(schema=Storyboard, model=gemini-2.5-pro)`
4. 字数校验（4s→15-18字 / 6s→22-25字 / 8s→30-34字），超限标记 `word_count_warning`
5. 返回 `Storyboard`，写盘由 worker 的 task 函数负责

#### Director（director.py）

输入：单个 `Shot` 记录、`GeminiProvider`
输出：`str`（运镜提示词）

后处理（强制）：若 `shot.text` 非空，追加 `角色说：『{shot.text}』`

#### VideoGenerator（video_generator.py）

输入：`motion_prompt: str`、`first_frame_path: str`、`shot_duration: int`、`spoken_text: str`、`genai.Client`
输出：`bytes`（mp4 视频内容）

轮询逻辑：每 10 秒检查一次，最多等待 300 秒，超时抛 `TimeoutError`。

```python
async def generate_video(
    client: genai.Client,
    motion_prompt: str,
    first_frame_path: str,
    shot_duration: int,
    spoken_text: str,
) -> bytes:
    operation = client.models.generate_videos(
        model="veo-3.0-generate-001",
        prompt=motion_prompt,
        image=types.Image.from_file(first_frame_path),
        config=types.GenerateVideosConfig(
            aspect_ratio="16:9",
            duration_seconds=shot_duration,
            generate_audio=True,
            spoken_text=spoken_text,
            number_of_videos=1,
        ),
    )
    elapsed = 0
    while not operation.done:
        if elapsed >= 300:
            raise TimeoutError("Veo 3 operation timed out after 5 minutes")
        await asyncio.sleep(10)
        elapsed += 10
        operation = client.operations.get(operation)
    return operation.response.generated_videos[0].video.video_bytes
```

#### FramePorter（frame_porter.py）

```python
import ffmpeg

def extract_last_frame(video_path: str, output_path: str) -> None:
    (
        ffmpeg
        .input(video_path, sseof=-0.1)
        .output(output_path, vframes=1, **{"q:v": 2})
        .overwrite_output()
        .run(quiet=True)
    )
```

#### Merger（merger.py）

```python
import ffmpeg
import tempfile
from pathlib import Path

def merge_shots(shot_paths: list[str], output_path: str) -> None:
    filelist = Path(tempfile.mktemp(suffix=".txt"))
    filelist.write_text("\n".join(f"file '{p}'" for p in shot_paths))
    (
        ffmpeg
        .input(str(filelist), format="concat", safe=0)
        .output(output_path, c="copy")
        .overwrite_output()
        .run(quiet=True)
    )
    filelist.unlink(missing_ok=True)
```

---

## 6. Worker 任务（worker/tasks.py）

arq 任务函数负责：调用 agents → 更新 SQLite → 发 Redis 事件。每个任务都是项目级别的独立单元。

### 6.1 run_screenwriter

```
1. 读 project + reference_images
2. 调 Screenwriter agent → Storyboard
3. 字数校验标记
4. storyboard.json 落盘
5. 事务：更新 projects.scene_overview + 批量 insert shots + 转态 SCRIPT_REVIEW
6. Redis PUBLISH {type: "script_ready", storyboard}
```

错误处理：Gemini API 错误自动重试 2 次（指数退避），JSON 校验失败重试 1 次，最终失败转态 `FAILED`。

### 6.2 run_shot_pipeline

```
for shot in shots WHERE status = PENDING ORDER BY shot_id ASC:
    a. shot.status = PROMPT_GENERATING
    b. Director agent → motion_prompt，落盘
    c. pick_first_frame(project, shot) 决定首帧图路径
    d. shot.first_frame_path = 实际使用的首帧路径
    e. shot.status = VIDEO_GENERATING
    f. VideoGenerator → mp4 bytes，写 output.mp4
    g. FramePorter → last_frame.png
    h. shot.status = COMPLETED
    i. Redis PUBLISH {type: "shot_completed", shot_id}
    -- 若任一步抛异常：shot.status = FAILED，break

转态 SHOT_REVIEW（无论全成功还是部分失败）
Redis PUBLISH {type: "all_shots_ready", has_failures}
```

**关键约束：**
- Shot 必须按 `shot_id` 升序串行处理，不允许并行
- 只处理 `status = PENDING` 的 shot，实现重跑幂等性
- 单镜失败不触发 `ProjectStatus.FAILED`，软失败回到 `SHOT_REVIEW`

### 6.3 首帧选择逻辑

```python
def pick_first_frame(project, shot, get_shot_fn, get_first_character_ref_fn) -> str:
    if shot.shot_id == 1 or not shot.align_with_previous:
        return get_first_character_ref_fn(project)
    prev = get_shot_fn(project.id, shot.shot_id - 1)
    return prev.last_frame_path
```

### 6.4 run_merger

```
1. 查所有 COMPLETED shots，按 shot_id 排序，取 video_path
2. 写 filelist.txt
3. ffmpeg concat → merged.mp4
4. 转态 EXPORTED
5. Redis PUBLISH {type: "export_done", download_url}
```

### 6.5 arq WorkerSettings（worker/arq_worker.py）

```python
from arq.connections import RedisSettings
from worker.tasks import run_screenwriter, run_shot_pipeline, run_merger
from app.config import settings

class WorkerSettings:
    functions = [run_screenwriter, run_shot_pipeline, run_merger]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = settings.worker_pool_size
    job_timeout = 1800  # 30 分钟，Veo 3 生成可能较慢
```

启动命令：`uv run arq worker.arq_worker.WorkerSettings`

---

## 7. API 端点

所有路由前缀 `/api`，统一错误格式 `{"error": {"code": "...", "message": "..."}}`.  
写操作必须携带 `X-User-Name` header（不做校验，仅存储）。

### 7.1 项目管理

| Method | Path | 状态码 | 说明 |
|---|---|---|---|
| `GET` | `/api/projects` | 200 | 列表，支持 `?status=&creator=&sort=created_at:desc&limit=20&offset=0` |
| `POST` | `/api/projects` | 201 | 创建，body `{title, theme_text}` → `{project_id, status: "draft"}` |
| `GET` | `/api/projects/{id}` | 200 | 详情（含 shots、reference_images、storyboard） |
| `DELETE` | `/api/projects/{id}` | 204 | 级联删 DB + 清空 `storage/projects/{id}/` |

### 7.2 参考图

| Method | Path | 说明 |
|---|---|---|
| `POST` | `/api/projects/{id}/reference-images` | multipart `files[]` + `kind` (character/scene) |
| `DELETE` | `/api/projects/{id}/reference-images/{image_id}` | 删除单张 |

### 7.3 Pipeline 触发与审批

| Method | Path | 前置状态 → 目标状态 | 说明 |
|---|---|---|---|
| `POST` | `/api/projects/{id}/start` | DRAFT → SCRIPTING | 校验 ≥1 张 character 图 |
| `POST` | `/api/projects/{id}/regenerate-script` | SCRIPT_REVIEW → SCRIPTING | 归档旧 storyboard，清空 shots |
| `PATCH` | `/api/projects/{id}/storyboard` | SCRIPT_REVIEW | 直接修改分镜内容（含 align_with_previous） |
| `PUT` | `/api/projects/{id}/storyboard` | SCRIPT_REVIEW | 全量替换分镜：upsert shots、删除缺失 shot（含目录）、重写 storyboard.json |
| `POST` | `/api/projects/{id}/approve-script` | SCRIPT_REVIEW → SHOT_GENERATING | |
| `POST` | `/api/projects/{id}/regenerate-shots` | SHOT_REVIEW → SHOT_GENERATING | body `{shot_ids: [...]}` |
| `PATCH` | `/api/projects/{id}/shots/{shot_id}` | SHOT_REVIEW | 编辑 motion_prompt，不自动重跑 |
| `POST` | `/api/projects/{id}/export` | SHOT_REVIEW → EXPORTING | 全部 shot 必须 COMPLETED |
| `POST` | `/api/projects/{id}/reset-to-script` | SHOT_REVIEW → SCRIPTING | 归档 storyboard，清空 shots |
| `POST` | `/api/projects/{id}/reset` | FAILED → DRAFT | 清空 shots，归档 storyboard，保留参考图 |

### 7.4 资源下载

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/api/projects/{id}/assets/{kind}/{file}` | 静态文件代理（reference_images / shots/shot_N / final） |
| `GET` | `/api/projects/{id}/final.mp4` | 直接下载成片 |

### 7.5 SSE 实时进度

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/api/projects/{id}/stream` | `text/event-stream`，首发 `state_snapshot`，之后增量推送 |

#### SSE 事件类型

| 事件类型 | payload | 触发时机 |
|---|---|---|
| `state_snapshot` | `{status, shots, storyboard}` | SSE 连接建立时初始快照 |
| `state_change` | `{from, to}` | 状态转换 |
| `script_ready` | `{storyboard}` | Screenwriter 完成 |
| `shot_started` | `{shot_id}` | Director 开始处理某镜 |
| `shot_progress` | `{shot_id, sub_status}` | Shot 进入 VIDEO_GENERATING |
| `shot_completed` | `{shot_id, preview_url, video_url}` | 单镜完成 |
| `shot_failed` | `{shot_id, error}` | 单镜失败 |
| `all_shots_ready` | `{has_failures}` | 所有 shot 处理完毕，进入 SHOT_REVIEW |
| `export_done` | `{download_url}` | 成片就绪 |
| `pipeline_failed` | `{reason}` | 不可恢复全局错误 |

---

## 8. 数据模型

### 8.1 projects 表

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | UUID v4 |
| `title` | TEXT NOT NULL | 项目名称 |
| `theme_text` | TEXT NOT NULL | 一句话主题 |
| `creator_name` | TEXT NOT NULL | 来自 X-User-Name header |
| `status` | TEXT NOT NULL | ProjectStatus 枚举字符串 |
| `scene_overview` | TEXT NULL | screenwriter 生成的场景概述 |
| `storyboard_path` | TEXT NULL | storyboard.json 相对路径 |
| `final_video_path` | TEXT NULL | merged.mp4 相对路径 |
| `error_message` | TEXT NULL | FAILED 时的错误详情 |
| `created_at` | DATETIME | |
| `updated_at` | DATETIME | |

索引：`(status)`、`(creator_name)`、`(created_at DESC)`

### 8.2 reference_images 表

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | UUID |
| `project_id` | TEXT FK → projects.id CASCADE | |
| `kind` | TEXT NOT NULL | `character` / `scene` |
| `filename` | TEXT NOT NULL | 原始文件名 |
| `storage_path` | TEXT NOT NULL | 相对路径 |
| `order_index` | INTEGER NOT NULL | 同 kind 内顺序 |
| `created_at` | DATETIME | |

索引：`(project_id, kind, order_index)`

### 8.3 shots 表

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `project_id` | TEXT FK → projects.id CASCADE | |
| `shot_id` | INTEGER NOT NULL | 分镜序号（从 1 开始） |
| `text` | TEXT NOT NULL | 台词 |
| `shot_type` | TEXT NOT NULL | Close-up / Medium Shot / Wide Shot |
| `visual_description` | TEXT NOT NULL | 动作与表情描述 |
| `shot_duration` | INTEGER NOT NULL | 4 / 6 / 8 秒 |
| `status` | TEXT NOT NULL | ShotStatus 枚举 |
| `align_with_previous` | BOOLEAN NOT NULL DEFAULT 1 | 是否与上一镜首尾帧对齐 |
| `motion_prompt` | TEXT NULL | director 生成的运镜提示词 |
| `first_frame_path` | TEXT NULL | 实际使用的首帧图路径 |
| `video_path` | TEXT NULL | output.mp4 相对路径 |
| `last_frame_path` | TEXT NULL | last_frame.png 相对路径 |
| `veo_operation_id` | TEXT NULL | Veo 3 操作 ID（调试用） |
| `word_count_warning` | BOOLEAN DEFAULT 0 | 台词字数超限标记 |
| `error_message` | TEXT NULL | 单镜失败时的错误 |
| `created_at` | DATETIME | |
| `updated_at` | DATETIME | |

索引：`(project_id, shot_id)` UNIQUE、`(project_id, status)`

### 8.4 events 表（审计）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `project_id` | TEXT FK | |
| `actor` | TEXT NOT NULL | `user:{name}` / `system:worker` |
| `event_type` | TEXT NOT NULL | state_change / shot_completed / error / ... |
| `payload` | TEXT (JSON) | 事件详情 |
| `created_at` | DATETIME | |

索引：`(project_id, created_at DESC)`

---

## 9. 错误与重试策略

| Agent | 失败类型 | 处理 |
|---|---|---|
| Screenwriter | Gemini API 5xx / timeout | 指数退避重试 2 次 → ProjectStatus.FAILED |
| Screenwriter | JSON 校验失败 | 重试 1 次（附格式纠正提示）→ FAILED |
| Director | Gemini API 瞬时错误 | 重试 2 次 → ShotStatus.FAILED，pipeline 暂停 |
| VideoGenerator | Veo API 非 200 | 重试 1 次 → ShotStatus.FAILED，pipeline 暂停 |
| VideoGenerator | 轮询超时 (>5 分钟) | ShotStatus.FAILED，不重试 |
| FramePorter | ffmpeg 非 0 退出 | 不重试 → ShotStatus.FAILED |
| Merger | ffmpeg 非 0 退出 | 不重试 → ProjectStatus.FAILED |

**软失败原则**：`ShotStatus.FAILED` 不级联为 `ProjectStatus.FAILED`。pipeline 停止处理剩余 shot，转态 `SHOT_REVIEW`，用户选择"只重跑失败的镜"。`ProjectStatus.FAILED` 仅由 Screenwriter / Merger 的不可恢复错误触发。

---

## 10. 并发规则

- **单项目串行**：同一 `project_id` 在任意时刻只有一个 worker task 运行。FastAPI 在触发前校验当前状态是否可合法转换，利用 SQLite 的 `status` 字段作为互斥锁。
- **跨项目并行**：多个项目的 pipeline 并行在 arq worker 池中执行（默认 `max_jobs=4`，可配）。
- **Shot 级串行**：`run_shot_pipeline` 内部按 `shot_id` 升序同步处理，不并行。原因：第 N 镜的首帧依赖第 N-1 镜的尾帧。

---

## 11. 测试策略

```bash
# 运行所有测试
uv run pytest

# 运行带覆盖率报告
uv run pytest --cov=app --cov-report=term-missing

# 只运行单元测试
uv run pytest tests/unit/ -v

# 运行集成测试
uv run pytest tests/integration/ -v

# 运行端到端烟雾测试（需真实 API 凭证）
E2E=1 uv run pytest tests/e2e/ -v
```

| 层级 | 框架 | 覆盖范围 |
|---|---|---|
| 单元测试 | pytest + pytest-asyncio | agents/、state_machine、Pydantic schemas |
| 集成测试 | pytest + httpx.AsyncClient + fakeredis | FastAPI 路由、SQLite 读写、SSE 流 |
| Agent mock 测试 | pytest + unittest.mock | 所有 Gemini / Veo 3 调用走 mock |
| 端到端 | pytest（E2E=1 开关） | 真实 API，1 镜头最短链路 |

**CI 门槛：** 单元 + 集成测试通过，覆盖率 ≥ 70%。

---

## 12. 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 数据库 | SQLite（aiosqlite） | 内网小团队，无高并发，零运维，够用 |
| 任务队列 | arq（非 Celery） | 纯 asyncio，和 FastAPI 生态契合，轻量 |
| Shot 执行顺序 | 强制串行 | 首尾帧依赖关系，不能并行 |
| 重跑幂等性 | 只处理 PENDING shot | 首次跑和重跑是同一段代码，简单可靠 |
| 软失败 | 单镜失败不终止项目 | 用户体验：不因一镜失败丢失所有已成功的镜 |
| 级联重跑 | 不自动级联 | 成本控制（Veo 3 昂贵）+ 尊重用户意图 |
| storyboard 存储 | JSON 文件 + SQLite 双存 | 文件归档 + DB 运行时查询，两者不冲突 |
| 鉴权 | 无鉴权（X-User-Name header） | 内网工具，简化 MVP 范围 |

---

## 13. MCP 服务：台词与动作创作代理

### 13.1 概述

`mcp` 服务是一个 **Model Context Protocol (MCP) 服务端**，专为外部 LLM 代理提供对项目分镜内容的读写接口，使代理能够批量创作台词（`text`）和动作提示词（`motion_prompt`），而无需直接访问数据库或了解 REST 细节。

**MCP 服务本身不调用任何 LLM**，所有 AI 能力由接入的外部代理（如 Claude、GPT-4o 等）提供。

### 13.2 PUT /api/projects/{id}/storyboard — 全量替换分镜

与现有 `PATCH /storyboard`（部分更新）不同，`PUT /storyboard` 执行**全量替换**语义：

| 特性 | PATCH /storyboard | PUT /storyboard |
|---|---|---|
| 字段 | 均可选 | `scene_overview` + `shots` 均必填 |
| Shot 处理 | 按 `shot_id` 更新已有 shot | upsert + 删除 payload 中不存在的 shot |
| 文件清理 | 不清理 | 删除多余 shot 对应的存储目录 |
| storyboard.json | 不重写 | 重写（与 DB 保持一致） |
| 前置状态 | `script_review` | `script_review` |
| 非法状态响应 | 409 | 409 |

**请求体（`StoryboardReplace`）：**

```json
{
  "scene_overview": "主人公在咖啡馆等待老朋友...",
  "shots": [
    {
      "shot_id": 1,
      "text": "你终于来了。",
      "shot_type": "Close-up",
      "visual_description": "主人公抬头，眼神中带着释然",
      "shot_duration": 4,
      "align_with_previous": false,
      "reference_image_hint": null
    }
  ]
}
```

**`ShotItem` 字段说明：**

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `shot_id` | int | 唯一 | 分镜序号（从 1 开始），payload 内不可重复 |
| `text` | str | 必填 | 台词内容 |
| `shot_type` | str | `Close-up`/`Medium Shot`/`Wide Shot` | 景别 |
| `visual_description` | str | 必填 | 动作与表情描述 |
| `shot_duration` | int | 4–8 | 时长（秒） |
| `align_with_previous` | bool | 默认 true | 是否与前一镜首尾帧对齐 |
| `reference_image_hint` | str | 可选 | 参考图提示（供后续处理使用） |

> **注意**：`ShotItem` 不包含 `motion_prompt`。`PUT /storyboard` 只负责结构与台词；动作提示词通过后续的 `PATCH /shots/{shot_id}` 或 MCP write 工具单独设置。

**文件清理（CLAUDE.md 素材审计）：**  
payload 中不存在的 `shot_id` 对应的数据库行会被删除，其 `storage/projects/{id}/shots/shot_{n}/` 目录也会被 `shutil.rmtree` 清除，防止读到过期素材文件。

### 13.3 MCP 服务部署

**Compose 服务**（`deploy/docker-compose.dev.yml`，服务名 `mcp`）：

```
┌──────────────────────────────────────────────────────────┐
│                       Docker Compose                     │
│                                                          │
│  ┌──────────────────┐  HTTP /api   ┌────────────────┐    │
│  │  外部 LLM 代理   │ ◄──────────► │  Backend API   │    │
│  │  (Claude 等)     │              │  :8002         │    │
│  └────────┬─────────┘              └────────────────┘    │
│           │ MCP (HTTP)                      ▲             │
│           ▼                                │             │
│  ┌──────────────────┐  REST (httpx)        │             │
│  │  mcp 容器        │ ────────────────────►│             │
│  │  :8765 /mcp      │                                    │
│  └──────────────────┘                                    │
└──────────────────────────────────────────────────────────┘
```

| 属性 | 值 |
|---|---|
| 镜像 | `video-maker-worker-dev`（与 `backend` / `worker` 同一镜像） |
| 端口 | `8765`（主机）→ `8765`（容器） |
| 传输协议 | HTTP，FastMCP 默认路径 `/mcp` |
| 入口 | `uv run --project . python -m mcp_server.server` |
| 配置环境变量 | `BACKEND_BASE_URL=http://video-maker-backend-dev:8002`、`MCP_HOST=0.0.0.0`、`MCP_PORT=8765` |
| 鉴权 | 无（信任网络内部，`X-User-Name: mcp-agent` 固定注入） |
| 依赖 | `backend` 服务（needs `backend` to be up） |

**启动命令：**

```bash
make dev-mcp   # 仅启动 mcp 服务（backend 须已运行）
```

### 13.4 代码模块（backend/mcp_server/）

| 文件 | 职责 |
|---|---|
| `server.py` | FastMCP 服务端入口，注册全部 9 个工具，`create_server(backend)` 工厂函数 |
| `client.py` | `BackendClient` — httpx 异步封装，`BackendError(status_code, detail)` 异常类 |
| `config.py` | `Settings` — 读取 `BACKEND_BASE_URL` / `MCP_HOST` / `MCP_PORT` 环境变量 |
| `validation.py` | `word_count_report(text, duration)` — 字数建议报告（复用 screenwriter 规则，仅提示不阻断） |
| `context.py` | `shape_project` / `shape_shot` / `with_neighbors` — 整形 API 响应为代理友好格式 |
| `guidelines.py` | `AUTHORING_GUIDELINES` 常量 — 台词与动作创作约定文本 |

### 13.5 两阶段创作流程

MCP 代理的推荐操作顺序：

```
阶段 1：结构 + 台词
  replace_storyboard(project_id, scene_overview, shots=[{shot_id, text, ...}, ...])
    → 全量写入分镜结构与台词（无 motion_prompt）

阶段 2：动作
  batch_update_shots(project_id, updates=[{shot_id, motion_prompt}, ...])
    → 批量写入各镜运镜提示词
```

也可以用 `update_dialogue` / `update_motion` 逐镜操作；`batch_update_shots` 支持同时传 `text` 和 `motion_prompt`，允许部分成功（`"ok": false` 逐项报告错误）。

工具目录详见 `backend/mcp_server/README.md`。
