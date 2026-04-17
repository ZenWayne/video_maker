# Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Video Maker Agent backend — a FastAPI + arq + SQLite + Redis system that orchestrates Gemini/Veo3 pipelines to produce short videos from a theme prompt.

**Architecture:** FastAPI handles REST + SSE; arq workers call Gemini (script/director) and Veo3 (video gen) sequentially per project; state transitions are enforced by a central state machine; Redis carries both the task queue and SSE event bus.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async (aiosqlite), arq, Redis, google-genai SDK (Vertex AI), ffmpeg-python, pydantic-settings, sse-starlette, uv

---

## File Map

| File | Responsibility |
|---|---|
| `backend/pyproject.toml` | uv dependency declaration |
| `backend/Dockerfile` | Container image |
| `backend/app/config.py` | Settings via pydantic-settings |
| `backend/app/db.py` | SQLAlchemy async engine + session factory |
| `backend/app/main.py` | FastAPI app, router mounts, lifespan |
| `backend/app/models/project.py` | ORM: Project, Shot, ReferenceImage, Event |
| `backend/app/models/schemas.py` | Pydantic request/response models |
| `backend/app/services/state_machine.py` | Enums + VALID_TRANSITIONS + transition() |
| `backend/app/services/storage.py` | Path generation functions |
| `backend/app/services/events.py` | Redis pub/sub: publish() + subscribe() |
| `backend/app/agents/llm.py` | GeminiProvider (generate_json / generate_text) |
| `backend/app/agents/screenwriter.py` | Multimodal → Storyboard Pydantic object |
| `backend/app/agents/director.py` | Shot → motion_prompt string |
| `backend/app/agents/video_generator.py` | Veo3 polling → mp4 bytes |
| `backend/app/agents/frame_porter.py` | ffmpeg: extract last frame |
| `backend/app/agents/merger.py` | ffmpeg: concat shots → merged.mp4 |
| `backend/app/api/projects.py` | CRUD routes for projects |
| `backend/app/api/pipeline.py` | Pipeline trigger + approval routes |
| `backend/app/api/uploads.py` | Reference image upload/delete |
| `backend/app/api/assets.py` | Static file proxy |
| `backend/app/api/stream.py` | SSE endpoint |
| `backend/worker/arq_worker.py` | arq WorkerSettings |
| `backend/worker/tasks.py` | run_screenwriter / run_shot_pipeline / run_merger |
| `backend/prompts/screenwriter.md` | Screenwriter system prompt |
| `backend/prompts/director.md` | Director system prompt |
| `backend/tests/conftest.py` | Shared fixtures |
| `backend/tests/unit/test_schemas.py` | Pydantic schema unit tests |
| `backend/tests/unit/test_state_machine.py` | State machine unit tests |
| `backend/tests/unit/test_agents.py` | Agent mock tests |
| `backend/tests/integration/test_projects_api.py` | Projects CRUD integration |
| `backend/tests/integration/test_pipeline_api.py` | Pipeline trigger integration |
| `backend/tests/integration/test_sse.py` | SSE stream integration |

---

### Task 1: Project Scaffold

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/Dockerfile`
- Create: `backend/.gitignore`
- Create: `backend/.python-version`

- [ ] **Step 1: Create backend directory and initialize uv project**

```bash
mkdir -p backend
cd backend
uv init --no-workspace
```

Expected: `pyproject.toml` and `.python-version` appear in `backend/`.

- [ ] **Step 2: Replace generated pyproject.toml with correct content**

Replace `backend/pyproject.toml` entirely:

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

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

- [ ] **Step 3: Install dependencies**

```bash
cd backend
uv sync --group dev
```

Expected: `uv.lock` created, `.venv/` created with all packages.

- [ ] **Step 4: Create Dockerfile**

Create `backend/Dockerfile`:

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN uv sync --no-dev --system

COPY . .
```

- [ ] **Step 5: Create .gitignore**

Create `backend/.gitignore`:

```
.venv/
__pycache__/
*.pyc
*.pyo
.pytest_cache/
.coverage
htmlcov/
storage/
metadata.db
.env
*.egg-info/
dist/
```

- [ ] **Step 6: Create package skeleton directories**

```bash
cd backend
mkdir -p app/models app/api app/services app/agents
mkdir -p worker prompts tests/unit tests/integration
touch app/__init__.py app/models/__init__.py app/api/__init__.py
touch app/services/__init__.py app/agents/__init__.py
touch worker/__init__.py tests/__init__.py tests/unit/__init__.py tests/integration/__init__.py
```

- [ ] **Step 7: Commit**

```bash
cd backend
git add pyproject.toml uv.lock .python-version Dockerfile .gitignore app worker tests prompts
git commit -m "feat: scaffold backend project with uv"
```

---

### Task 2: Config & Database

**Files:**
- Create: `backend/app/config.py`
- Create: `backend/app/db.py`

- [ ] **Step 1: Write config.py**

Create `backend/app/config.py`:

```python
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    gcp_project: str = "local"
    gcp_location: str = "us-central1"
    google_application_credentials: str = "/secrets/vertex.json"
    redis_url: str = "redis://localhost:6379"
    storage_root: str = "/tmp/video_maker_storage"
    database_url: str = "sqlite+aiosqlite:////tmp/video_maker.db"
    gemini_script_model: str = "gemini-2.5-pro"
    gemini_director_model: str = "gemini-2.5-flash"
    worker_pool_size: int = 4
    veo_poll_interval_seconds: int = 10
    veo_max_wait_seconds: int = 300

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
```

- [ ] **Step 2: Write db.py**

Create `backend/app/db.py`:

```python
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from typing import AsyncGenerator
from app.config import settings

engine = create_async_engine(settings.database_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def create_tables() -> None:
    from app.models.project import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
```

- [ ] **Step 3: Commit**

```bash
cd backend
git add app/config.py app/db.py
git commit -m "feat: add config and async DB session factory"
```

---

### Task 3: ORM Models

**Files:**
- Create: `backend/app/models/project.py`

- [ ] **Step 1: Write ORM models**

Create `backend/app/models/project.py`:

```python
import uuid
from datetime import datetime
from sqlalchemy import String, Text, Boolean, Integer, DateTime, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        Index("ix_projects_status", "status"),
        Index("ix_projects_creator", "creator_name"),
        Index("ix_projects_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    theme_text: Mapped[str] = mapped_column(Text, nullable=False)
    creator_name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    scene_overview: Mapped[str | None] = mapped_column(Text, nullable=True)
    storyboard_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_video_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    shots: Mapped[list["Shot"]] = relationship("Shot", back_populates="project", cascade="all, delete-orphan", order_by="Shot.shot_id")
    reference_images: Mapped[list["ReferenceImage"]] = relationship("ReferenceImage", back_populates="project", cascade="all, delete-orphan")
    events: Mapped[list["Event"]] = relationship("Event", back_populates="project", cascade="all, delete-orphan")


class Shot(Base):
    __tablename__ = "shots"
    __table_args__ = (
        UniqueConstraint("project_id", "shot_id", name="uq_shots_project_shot"),
        Index("ix_shots_project_status", "project_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    shot_id: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    shot_type: Mapped[str] = mapped_column(String, nullable=False)
    visual_description: Mapped[str] = mapped_column(Text, nullable=False)
    shot_duration: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    align_with_previous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    motion_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_frame_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_frame_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    veo_operation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    word_count_warning: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project: Mapped["Project"] = relationship("Project", back_populates="shots")


class ReferenceImage(Base):
    __tablename__ = "reference_images"
    __table_args__ = (
        Index("ix_ref_images_project_kind", "project_id", "kind", "order_index"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # "character" | "scene"
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    project: Mapped["Project"] = relationship("Project", back_populates="reference_images")


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_project_created", "project_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON string
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    project: Mapped["Project"] = relationship("Project", back_populates="events")
```

- [ ] **Step 2: Commit**

```bash
cd backend
git add app/models/project.py
git commit -m "feat: add SQLAlchemy ORM models"
```

---

### Task 4: Pydantic Schemas + Unit Tests

**Files:**
- Create: `backend/app/models/schemas.py`
- Create: `backend/tests/unit/test_schemas.py`

- [ ] **Step 1: Write failing tests**

Create `backend/tests/unit/test_schemas.py`:

```python
import pytest
from app.models.schemas import (
    ProjectCreate, ProjectResponse, ShotResponse,
    ReferenceImageResponse, StoryboardShotItem, StoryboardPatch,
    ShotPatch, RegenerateShotsRequest,
)


def test_project_create_valid():
    p = ProjectCreate(title="My Video", theme_text="A story about AI")
    assert p.title == "My Video"
    assert p.theme_text == "A story about AI"


def test_project_create_missing_fields():
    with pytest.raises(Exception):
        ProjectCreate(title="Only title")


def test_storyboard_shot_item_valid():
    item = StoryboardShotItem(
        shot_id=1,
        text="Hello world",
        shot_type="Close-up",
        visual_description="Character smiles",
        shot_duration=4,
        align_with_previous=False,
    )
    assert item.shot_duration in (4, 6, 8)


def test_storyboard_shot_item_invalid_duration():
    with pytest.raises(Exception):
        StoryboardShotItem(
            shot_id=1,
            text="x",
            shot_type="Close-up",
            visual_description="y",
            shot_duration=5,  # invalid
            align_with_previous=True,
        )


def test_regenerate_shots_request():
    r = RegenerateShotsRequest(shot_ids=[1, 2, 3])
    assert r.shot_ids == [1, 2, 3]


def test_shot_patch_partial():
    p = ShotPatch(motion_prompt="pan left slowly")
    assert p.motion_prompt == "pan left slowly"
    assert p.align_with_previous is None
```

- [ ] **Step 2: Run tests — expect import errors**

```bash
cd backend
uv run pytest tests/unit/test_schemas.py -v
```

Expected: `ImportError` — `app.models.schemas` does not exist yet.

- [ ] **Step 3: Write schemas.py**

Create `backend/app/models/schemas.py`:

```python
from __future__ import annotations
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, field_validator


class ProjectCreate(BaseModel):
    title: str
    theme_text: str


class ReferenceImageResponse(BaseModel):
    id: str
    kind: str
    filename: str
    storage_path: str
    order_index: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ShotResponse(BaseModel):
    id: int
    shot_id: int
    text: str
    shot_type: str
    visual_description: str
    shot_duration: int
    status: str
    align_with_previous: bool
    motion_prompt: Optional[str]
    first_frame_path: Optional[str]
    video_path: Optional[str]
    last_frame_path: Optional[str]
    word_count_warning: bool
    error_message: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProjectResponse(BaseModel):
    id: str
    title: str
    theme_text: str
    creator_name: str
    status: str
    scene_overview: Optional[str]
    storyboard_path: Optional[str]
    final_video_path: Optional[str]
    error_message: Optional[str]
    created_at: datetime
    updated_at: datetime
    shots: list[ShotResponse] = []
    reference_images: list[ReferenceImageResponse] = []

    model_config = {"from_attributes": True}


class ProjectListItem(BaseModel):
    id: str
    title: str
    status: str
    creator_name: str
    created_at: datetime

    model_config = {"from_attributes": True}


class StoryboardShotItem(BaseModel):
    shot_id: int
    text: str
    shot_type: str
    visual_description: str
    shot_duration: Literal[4, 6, 8]
    align_with_previous: bool = True

    @field_validator("shot_duration")
    @classmethod
    def validate_duration(cls, v: int) -> int:
        if v not in (4, 6, 8):
            raise ValueError("shot_duration must be 4, 6, or 8")
        return v


class StoryboardPatch(BaseModel):
    scene_overview: Optional[str] = None
    shots: Optional[list[StoryboardShotItem]] = None


class ShotPatch(BaseModel):
    motion_prompt: Optional[str] = None
    align_with_previous: Optional[bool] = None


class RegenerateShotsRequest(BaseModel):
    shot_ids: list[int]


class ErrorResponse(BaseModel):
    error: dict[str, str]
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
cd backend
uv run pytest tests/unit/test_schemas.py -v
```

Expected: All 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd backend
git add app/models/schemas.py tests/unit/test_schemas.py
git commit -m "feat: add Pydantic schemas with unit tests"
```

---

### Task 5: State Machine + Unit Tests

**Files:**
- Create: `backend/app/services/state_machine.py`
- Create: `backend/tests/unit/test_state_machine.py`

- [ ] **Step 1: Write failing tests**

Create `backend/tests/unit/test_state_machine.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.state_machine import (
    ProjectStatus, ShotStatus, VALID_TRANSITIONS,
    InvalidTransitionError, transition,
)


def test_all_project_statuses_have_transition_entry():
    for status in ProjectStatus:
        if status != ProjectStatus.EXPORTED:
            assert status in VALID_TRANSITIONS


def test_valid_transitions_draft_to_scripting():
    assert ProjectStatus.SCRIPTING in VALID_TRANSITIONS[ProjectStatus.DRAFT]


def test_invalid_transition_draft_to_exporting():
    assert ProjectStatus.EXPORTING not in VALID_TRANSITIONS[ProjectStatus.DRAFT]


def test_shot_status_enum_values():
    assert ShotStatus.PENDING.value == "pending"
    assert ShotStatus.COMPLETED.value == "completed"
    assert ShotStatus.FAILED.value == "failed"


@pytest.mark.asyncio
async def test_transition_valid():
    project = MagicMock()
    project.status = "draft"
    project.id = "proj-1"
    session = AsyncMock()
    redis = AsyncMock()

    with patch("app.services.state_machine.publish", new_callable=AsyncMock) as mock_pub:
        await transition(project, ProjectStatus.SCRIPTING, "user:alice", session, redis)

    assert project.status == "scripting"
    session.add.assert_called()
    session.commit.assert_awaited()
    mock_pub.assert_awaited_once()


@pytest.mark.asyncio
async def test_transition_invalid_raises():
    project = MagicMock()
    project.status = "draft"
    session = AsyncMock()
    redis = AsyncMock()

    with pytest.raises(InvalidTransitionError):
        await transition(project, ProjectStatus.EXPORTING, "user:alice", session, redis)
```

- [ ] **Step 2: Run tests — expect import errors**

```bash
cd backend
uv run pytest tests/unit/test_state_machine.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Write state_machine.py**

Create `backend/app/services/state_machine.py`:

```python
from enum import Enum
import json
from datetime import datetime
from app.models.project import Event


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


async def transition(project, target: ProjectStatus, actor: str, session, redis) -> None:
    """Validate → update SQLite → write audit event → publish Redis event."""
    from app.services.events import publish

    current = ProjectStatus(project.status)
    allowed = VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidTransitionError(
            f"Cannot transition from {current.value!r} to {target.value!r}"
        )

    previous = project.status
    project.status = target.value
    project.updated_at = datetime.utcnow()
    session.add(project)

    audit = Event(
        project_id=project.id,
        actor=actor,
        event_type="state_change",
        payload=json.dumps({"from": previous, "to": target.value}),
        created_at=datetime.utcnow(),
    )
    session.add(audit)
    await session.commit()

    await publish(redis, project.id, {
        "type": "state_change",
        "from": previous,
        "to": target.value,
    })
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
cd backend
uv run pytest tests/unit/test_state_machine.py -v
```

Expected: All 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd backend
git add app/services/state_machine.py tests/unit/test_state_machine.py
git commit -m "feat: state machine with enum, transitions, and audit events"
```

---

### Task 6: Storage & Events Services

**Files:**
- Create: `backend/app/services/storage.py`
- Create: `backend/app/services/events.py`

- [ ] **Step 1: Write storage.py**

Create `backend/app/services/storage.py`:

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

- [ ] **Step 2: Write events.py**

Create `backend/app/services/events.py`:

```python
import json
import redis.asyncio as aioredis
from typing import AsyncGenerator


async def publish(redis_client, project_id: str, event: dict) -> None:
    channel = f"events:{project_id}"
    await redis_client.publish(channel, json.dumps(event))


async def subscribe(redis_client, project_id: str) -> AsyncGenerator[dict, None]:
    """Async generator yielding parsed event dicts. Used by SSE endpoint."""
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(f"events:{project_id}")
    async for message in pubsub.listen():
        if message["type"] == "message":
            yield json.loads(message["data"])
```

- [ ] **Step 3: Commit**

```bash
cd backend
git add app/services/storage.py app/services/events.py
git commit -m "feat: storage path helpers and Redis pub/sub wrappers"
```

---

### Task 7: Agents

**Files:**
- Create: `backend/app/agents/llm.py`
- Create: `backend/app/agents/screenwriter.py`
- Create: `backend/app/agents/director.py`
- Create: `backend/app/agents/video_generator.py`
- Create: `backend/app/agents/frame_porter.py`
- Create: `backend/app/agents/merger.py`
- Create: `backend/tests/unit/test_agents.py`

- [ ] **Step 1: Write failing agent tests**

Create `backend/tests/unit/test_agents.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path


@pytest.mark.asyncio
async def test_screenwriter_returns_storyboard():
    from app.agents.screenwriter import run_screenwriter
    from app.models.schemas import StoryboardShotItem

    mock_provider = AsyncMock()
    mock_provider.generate_json = AsyncMock(return_value={
        "scene_overview": "A hero's journey",
        "shots": [
            {
                "shot_id": 1,
                "text": "Hello world",
                "shot_type": "Close-up",
                "visual_description": "Character looks forward",
                "shot_duration": 4,
                "align_with_previous": False,
            }
        ],
    })

    mock_project = MagicMock()
    mock_project.theme_text = "A story"
    mock_project.id = "proj-1"

    result = await run_screenwriter(mock_project, [], mock_provider, "fake-model", Path("/tmp/sp.md"))
    assert result.scene_overview == "A hero's journey"
    assert len(result.shots) == 1
    assert result.shots[0].shot_id == 1


@pytest.mark.asyncio
async def test_director_appends_spoken_text():
    from app.agents.director import run_director

    mock_provider = AsyncMock()
    mock_provider.generate_text = AsyncMock(return_value="Slow pan right")

    mock_shot = MagicMock()
    mock_shot.text = "你好世界"
    mock_shot.shot_type = "Close-up"
    mock_shot.visual_description = "Smiling"
    mock_shot.shot_duration = 4

    result = await run_director(mock_shot, mock_provider, "fake-model", Path("/tmp/dp.md"))
    assert "Slow pan right" in result
    assert "你好世界" in result


@pytest.mark.asyncio
async def test_director_no_text_no_append():
    from app.agents.director import run_director

    mock_provider = AsyncMock()
    mock_provider.generate_text = AsyncMock(return_value="Zoom in slowly")

    mock_shot = MagicMock()
    mock_shot.text = ""
    mock_shot.shot_type = "Wide Shot"
    mock_shot.visual_description = "Landscape"
    mock_shot.shot_duration = 6

    result = await run_director(mock_shot, mock_provider, "fake-model", Path("/tmp/dp.md"))
    assert result == "Zoom in slowly"
    assert "角色说" not in result


@pytest.mark.asyncio
async def test_video_generator_timeout():
    from app.agents.video_generator import generate_video

    mock_client = MagicMock()
    mock_operation = MagicMock()
    mock_operation.done = False
    mock_client.models.generate_videos.return_value = mock_operation
    mock_client.operations.get.return_value = mock_operation

    with patch("app.agents.video_generator.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(TimeoutError):
            await generate_video(
                client=mock_client,
                motion_prompt="pan left",
                first_frame_path="/tmp/frame.png",
                shot_duration=4,
                spoken_text="hello",
                poll_interval=1,
                max_wait=5,
            )


def test_frame_porter_calls_ffmpeg():
    from app.agents.frame_porter import extract_last_frame

    with patch("app.agents.frame_porter.ffmpeg") as mock_ffmpeg:
        mock_stream = MagicMock()
        mock_ffmpeg.input.return_value = mock_stream
        mock_stream.output.return_value = mock_stream
        mock_stream.overwrite_output.return_value = mock_stream

        extract_last_frame("/tmp/video.mp4", "/tmp/last.png")

        mock_ffmpeg.input.assert_called_once_with("/tmp/video.mp4", sseof=-0.1)


def test_merger_calls_ffmpeg():
    from app.agents.merger import merge_shots

    with patch("app.agents.merger.ffmpeg") as mock_ffmpeg, \
         patch("app.agents.merger.Path") as mock_path, \
         patch("app.agents.merger.tempfile.mktemp", return_value="/tmp/filelist.txt"):
        mock_path.return_value.write_text = MagicMock()
        mock_path.return_value.unlink = MagicMock()
        mock_stream = MagicMock()
        mock_ffmpeg.input.return_value = mock_stream
        mock_stream.output.return_value = mock_stream
        mock_stream.overwrite_output.return_value = mock_stream

        merge_shots(["/tmp/shot1.mp4", "/tmp/shot2.mp4"], "/tmp/merged.mp4")

        mock_ffmpeg.input.assert_called_once()
```

- [ ] **Step 2: Run tests — expect import errors**

```bash
cd backend
uv run pytest tests/unit/test_agents.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Write llm.py**

Create `backend/app/agents/llm.py`:

```python
from google import genai
from google.genai import types


class GeminiProvider:
    def __init__(self, project: str, location: str, credentials_path: str):
        self.client = genai.Client(
            vertexai=True,
            project=project,
            location=location,
        )

    async def generate_json(
        self,
        model: str,
        system_prompt: str,
        user_parts: list,
        response_schema: type,
    ) -> dict:
        response = await self.client.aio.models.generate_content(
            model=model,
            contents=user_parts,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=response_schema,
            ),
        )
        import json
        return json.loads(response.text)

    async def generate_text(
        self,
        model: str,
        system_prompt: str,
        user_message: str,
    ) -> str:
        response = await self.client.aio.models.generate_content(
            model=model,
            contents=user_message,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
            ),
        )
        return response.text.strip()
```

- [ ] **Step 4: Write screenwriter.py**

Create `backend/app/agents/screenwriter.py`:

```python
from pathlib import Path
from pydantic import BaseModel
from app.models.schemas import StoryboardShotItem


class Storyboard(BaseModel):
    scene_overview: str
    shots: list[StoryboardShotItem]


# Word count limits per duration (Chinese characters)
WORD_COUNT_LIMITS = {4: (15, 18), 6: (22, 25), 8: (30, 34)}


async def run_screenwriter(
    project,
    reference_images: list,  # list of (bytes, label) tuples
    provider,
    model: str,
    system_prompt_path: Path,
) -> Storyboard:
    from google.genai import types as genai_types

    system_prompt = system_prompt_path.read_text(encoding="utf-8")

    user_parts = []
    for img_bytes, label in reference_images:
        user_parts.append(genai_types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))
        user_parts.append(f"[Reference: {label}]")
    user_parts.append(f"Theme: {project.theme_text}")

    raw = await provider.generate_json(
        model=model,
        system_prompt=system_prompt,
        user_parts=user_parts,
        response_schema=Storyboard,
    )

    storyboard = Storyboard(**raw)

    for shot in storyboard.shots:
        lo, hi = WORD_COUNT_LIMITS.get(shot.shot_duration, (0, 999))
        char_count = len(shot.text.replace(" ", ""))
        shot.word_count_warning = not (lo <= char_count <= hi)

    return storyboard
```

- [ ] **Step 5: Write director.py**

Create `backend/app/agents/director.py`:

```python
from pathlib import Path


async def run_director(shot, provider, model: str, system_prompt_path: Path) -> str:
    system_prompt = system_prompt_path.read_text(encoding="utf-8")

    user_message = (
        f"Shot type: {shot.shot_type}\n"
        f"Visual description: {shot.visual_description}\n"
        f"Duration: {shot.shot_duration}s\n"
    )

    motion_prompt = await provider.generate_text(
        model=model,
        system_prompt=system_prompt,
        user_message=user_message,
    )

    if shot.text:
        motion_prompt = f"{motion_prompt}\n角色说：『{shot.text}』"

    return motion_prompt
```

- [ ] **Step 6: Write video_generator.py**

Create `backend/app/agents/video_generator.py`:

```python
import asyncio
from google.genai import types


async def generate_video(
    client,
    motion_prompt: str,
    first_frame_path: str,
    shot_duration: int,
    spoken_text: str,
    poll_interval: int = 10,
    max_wait: int = 300,
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
        if elapsed >= max_wait:
            raise TimeoutError(f"Veo 3 operation timed out after {max_wait} seconds")
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        operation = client.operations.get(operation)

    return operation.response.generated_videos[0].video.video_bytes
```

- [ ] **Step 7: Write frame_porter.py**

Create `backend/app/agents/frame_porter.py`:

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

- [ ] **Step 8: Write merger.py**

Create `backend/app/agents/merger.py`:

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

- [ ] **Step 9: Run agent tests — expect PASS**

```bash
cd backend
uv run pytest tests/unit/test_agents.py -v
```

Expected: All 6 tests PASS.

- [ ] **Step 10: Commit**

```bash
cd backend
git add app/agents/ tests/unit/test_agents.py
git commit -m "feat: LLM provider + all pipeline agents with mock tests"
```

---

### Task 8: FastAPI App + Projects API + Test Fixtures

**Files:**
- Create: `backend/app/main.py`
- Create: `backend/app/api/projects.py`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/integration/test_projects_api.py`

- [ ] **Step 1: Write conftest.py (shared test fixtures)**

Create `backend/tests/conftest.py`:

```python
import pytest
import pytest_asyncio
import fakeredis.aioredis
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.main import app
from app.db import get_session
from app.models.project import Base

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def fake_redis():
    return fakeredis.aioredis.FakeRedis()


@pytest_asyncio.fixture
async def client(db_engine, fake_redis):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_session():
        async with factory() as session:
            yield session

    async def override_get_redis():
        return fake_redis

    app.dependency_overrides[get_session] = override_get_session
    from app.main import get_redis
    app.dependency_overrides[get_redis] = override_get_redis

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

    app.dependency_overrides.clear()
```

- [ ] **Step 2: Write failing integration tests for projects API**

Create `backend/tests/integration/test_projects_api.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_create_project(client):
    resp = await client.post(
        "/api/projects",
        json={"title": "My Video", "theme_text": "AI in 2026"},
        headers={"X-User-Name": "alice"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "draft"
    assert data["title"] == "My Video"
    assert "id" in data


@pytest.mark.asyncio
async def test_list_projects_empty(client):
    resp = await client.get("/api/projects")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_get_project_not_found(client):
    resp = await client.get("/api/projects/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_project(client, tmp_path, monkeypatch):
    import app.services.storage as storage_mod
    monkeypatch.setattr(storage_mod, "project_dir", lambda pid: tmp_path / pid)

    resp = await client.post(
        "/api/projects",
        json={"title": "Del Me", "theme_text": "x"},
        headers={"X-User-Name": "bob"},
    )
    pid = resp.json()["id"]

    resp = await client.delete(f"/api/projects/{pid}")
    assert resp.status_code == 204

    resp = await client.get(f"/api/projects/{pid}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_projects_filters(client):
    for i in range(3):
        await client.post(
            "/api/projects",
            json={"title": f"Video {i}", "theme_text": "x"},
            headers={"X-User-Name": "alice"},
        )
    await client.post(
        "/api/projects",
        json={"title": "Bob video", "theme_text": "y"},
        headers={"X-User-Name": "bob"},
    )

    resp = await client.get("/api/projects?creator=alice")
    assert resp.status_code == 200
    assert len(resp.json()) == 3

    resp = await client.get("/api/projects?limit=2")
    assert len(resp.json()) == 2
```

- [ ] **Step 3: Run tests — expect failures**

```bash
cd backend
uv run pytest tests/integration/test_projects_api.py -v
```

Expected: Failures because `app.main` doesn't exist yet.

- [ ] **Step 4: Write app/main.py**

Create `backend/app/main.py`:

```python
import redis.asyncio as aioredis
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import create_tables


_redis_pool: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    return _redis_pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis_pool
    await create_tables()
    _redis_pool = await aioredis.from_url(settings.redis_url, decode_responses=True)
    yield
    await _redis_pool.aclose()


app = FastAPI(title="Video Maker API", lifespan=lifespan)


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": str(exc)}},
    )


from app.api import projects, pipeline, uploads, assets, stream  # noqa: E402
app.include_router(projects.router, prefix="/api")
app.include_router(pipeline.router, prefix="/api")
app.include_router(uploads.router, prefix="/api")
app.include_router(assets.router, prefix="/api")
app.include_router(stream.router, prefix="/api")
```

- [ ] **Step 5: Write app/api/projects.py**

Create `backend/app/api/projects.py`:

```python
import shutil
from fastapi import APIRouter, Depends, HTTPException, Header, Query
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.project import Project
from app.models.schemas import ProjectCreate, ProjectResponse, ProjectListItem
from app.services.storage import project_dir

router = APIRouter()


def _require_user(x_user_name: str | None = Header(default=None)) -> str:
    if not x_user_name:
        raise HTTPException(status_code=400, detail="X-User-Name header required")
    return x_user_name


@router.get("/projects", response_model=list[ProjectListItem])
async def list_projects(
    status: str | None = Query(default=None),
    creator: str | None = Query(default=None),
    sort: str = Query(default="created_at:desc"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    q = select(Project)
    if status:
        q = q.where(Project.status == status)
    if creator:
        q = q.where(Project.creator_name == creator)
    q = q.order_by(Project.created_at.desc()).limit(limit).offset(offset)
    result = await session.execute(q)
    return result.scalars().all()


@router.post("/projects", response_model=ProjectResponse, status_code=201)
async def create_project(
    body: ProjectCreate,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    project = Project(
        title=body.title,
        theme_text=body.theme_text,
        creator_name=user,
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: str,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(
    project_id: str,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    await session.delete(project)
    await session.commit()

    storage = project_dir(project_id)
    if storage.exists():
        shutil.rmtree(storage, ignore_errors=True)
```

- [ ] **Step 6: Create stub files for remaining routers so imports work**

Create `backend/app/api/pipeline.py`:

```python
from fastapi import APIRouter
router = APIRouter()
```

Create `backend/app/api/uploads.py`:

```python
from fastapi import APIRouter
router = APIRouter()
```

Create `backend/app/api/assets.py`:

```python
from fastapi import APIRouter
router = APIRouter()
```

Create `backend/app/api/stream.py`:

```python
from fastapi import APIRouter
router = APIRouter()
```

- [ ] **Step 7: Run integration tests — expect PASS**

```bash
cd backend
uv run pytest tests/integration/test_projects_api.py -v
```

Expected: All 5 tests PASS.

- [ ] **Step 8: Commit**

```bash
cd backend
git add app/main.py app/api/ tests/conftest.py tests/integration/test_projects_api.py
git commit -m "feat: FastAPI app + projects CRUD API with integration tests"
```

---

### Task 9: Uploads & Assets API

**Files:**
- Modify: `backend/app/api/uploads.py`
- Modify: `backend/app/api/assets.py`
- Create: `backend/tests/integration/test_uploads_api.py`

- [ ] **Step 1: Write failing tests**

Create `backend/tests/integration/test_uploads_api.py`:

```python
import pytest
import io


@pytest.mark.asyncio
async def test_upload_reference_image(client, tmp_path, monkeypatch):
    import app.services.storage as storage_mod
    monkeypatch.setattr(storage_mod, "reference_images_dir", lambda pid: tmp_path / pid)

    # create project first
    resp = await client.post(
        "/api/projects",
        json={"title": "Test", "theme_text": "x"},
        headers={"X-User-Name": "alice"},
    )
    pid = resp.json()["id"]
    (tmp_path / pid).mkdir(parents=True, exist_ok=True)

    img_bytes = b"\x89PNG\r\n" + b"\x00" * 100  # fake PNG
    resp = await client.post(
        f"/api/projects/{pid}/reference-images",
        files={"files": ("face.png", io.BytesIO(img_bytes), "image/png")},
        data={"kind": "character"},
        headers={"X-User-Name": "alice"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert len(data) == 1
    assert data[0]["kind"] == "character"
    assert data[0]["filename"] == "face.png"


@pytest.mark.asyncio
async def test_delete_reference_image(client, tmp_path, monkeypatch):
    import app.services.storage as storage_mod
    monkeypatch.setattr(storage_mod, "reference_images_dir", lambda pid: tmp_path / pid)

    resp = await client.post(
        "/api/projects",
        json={"title": "T", "theme_text": "x"},
        headers={"X-User-Name": "alice"},
    )
    pid = resp.json()["id"]
    (tmp_path / pid).mkdir(parents=True, exist_ok=True)

    img_bytes = b"\x89PNG\r\n" + b"\x00" * 100
    upload = await client.post(
        f"/api/projects/{pid}/reference-images",
        files={"files": ("img.png", io.BytesIO(img_bytes), "image/png")},
        data={"kind": "scene"},
        headers={"X-User-Name": "alice"},
    )
    img_id = upload.json()[0]["id"]

    resp = await client.delete(f"/api/projects/{pid}/reference-images/{img_id}")
    assert resp.status_code == 204
```

- [ ] **Step 2: Run tests — expect failures**

```bash
cd backend
uv run pytest tests/integration/test_uploads_api.py -v
```

Expected: 404 or routing errors because uploads.py is a stub.

- [ ] **Step 3: Implement uploads.py**

Replace `backend/app/api/uploads.py`:

```python
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.project import Project, ReferenceImage
from app.models.schemas import ReferenceImageResponse
from app.services.storage import reference_images_dir

router = APIRouter()


@router.post("/projects/{project_id}/reference-images", response_model=list[ReferenceImageResponse], status_code=201)
async def upload_reference_images(
    project_id: str,
    kind: str = Form(...),
    files: list[UploadFile] = File(...),
    session: AsyncSession = Depends(get_session),
):
    if kind not in ("character", "scene"):
        raise HTTPException(status_code=400, detail="kind must be 'character' or 'scene'")

    result = await session.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    dest_dir = reference_images_dir(project_id)
    dest_dir.mkdir(parents=True, exist_ok=True)

    existing = await session.execute(
        select(ReferenceImage).where(
            ReferenceImage.project_id == project_id,
            ReferenceImage.kind == kind,
        )
    )
    current_count = len(existing.scalars().all())

    created = []
    for idx, upload in enumerate(files):
        content = await upload.read()
        safe_name = Path(upload.filename).name
        dest_path = dest_dir / safe_name
        dest_path.write_bytes(content)

        img = ReferenceImage(
            project_id=project_id,
            kind=kind,
            filename=safe_name,
            storage_path=str(dest_path),
            order_index=current_count + idx,
        )
        session.add(img)
        created.append(img)

    await session.commit()
    for img in created:
        await session.refresh(img)
    return created


@router.delete("/projects/{project_id}/reference-images/{image_id}", status_code=204)
async def delete_reference_image(
    project_id: str,
    image_id: str,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(ReferenceImage).where(
            ReferenceImage.id == image_id,
            ReferenceImage.project_id == project_id,
        )
    )
    img = result.scalar_one_or_none()
    if img is None:
        raise HTTPException(status_code=404, detail="Image not found")

    storage = Path(img.storage_path)
    if storage.exists():
        storage.unlink(missing_ok=True)

    await session.delete(img)
    await session.commit()
```

- [ ] **Step 4: Implement assets.py**

Replace `backend/app/api/assets.py`:

```python
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.services.storage import reference_images_dir, shot_dir, final_video_path

router = APIRouter()


@router.get("/projects/{project_id}/assets/{kind}/{file}")
async def serve_asset(project_id: str, kind: str, file: str):
    if kind == "reference_images":
        path = reference_images_dir(project_id) / file
    elif kind.startswith("shots/"):
        shot_part = kind.split("/", 1)[1]
        path = shot_dir(project_id, int(shot_part.replace("shot_", ""))) / file
    elif kind == "final":
        path = final_video_path(project_id).parent / file
    else:
        raise HTTPException(status_code=400, detail="Unknown asset kind")

    if not path.exists():
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(str(path))


@router.get("/projects/{project_id}/final.mp4")
async def download_final(project_id: str):
    path = final_video_path(project_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Final video not ready")
    return FileResponse(str(path), media_type="video/mp4", filename="merged.mp4")
```

- [ ] **Step 5: Run tests — expect PASS**

```bash
cd backend
uv run pytest tests/integration/test_uploads_api.py -v
```

Expected: Both tests PASS.

- [ ] **Step 6: Commit**

```bash
cd backend
git add app/api/uploads.py app/api/assets.py tests/integration/test_uploads_api.py
git commit -m "feat: reference image upload/delete and asset serving endpoints"
```

---

### Task 10: Pipeline API

**Files:**
- Modify: `backend/app/api/pipeline.py`
- Create: `backend/tests/integration/test_pipeline_api.py`

- [ ] **Step 1: Write failing tests**

Create `backend/tests/integration/test_pipeline_api.py`:

```python
import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_start_project_no_character_image(client):
    resp = await client.post(
        "/api/projects",
        json={"title": "T", "theme_text": "x"},
        headers={"X-User-Name": "alice"},
    )
    pid = resp.json()["id"]

    resp = await client.post(f"/api/projects/{pid}/start", headers={"X-User-Name": "alice"})
    assert resp.status_code == 400
    assert "character" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_start_project_invalid_status(client, tmp_path, monkeypatch):
    import app.services.storage as s
    monkeypatch.setattr(s, "reference_images_dir", lambda pid: tmp_path / pid)

    resp = await client.post(
        "/api/projects",
        json={"title": "T", "theme_text": "x"},
        headers={"X-User-Name": "alice"},
    )
    pid = resp.json()["id"]
    (tmp_path / pid).mkdir(parents=True, exist_ok=True)

    import io
    await client.post(
        f"/api/projects/{pid}/reference-images",
        files={"files": ("face.png", io.BytesIO(b"\x89PNG" + b"\x00" * 50), "image/png")},
        data={"kind": "character"},
        headers={"X-User-Name": "alice"},
    )

    with patch("app.api.pipeline.ArqRedis") as mock_arq:
        mock_arq.return_value.enqueue_job = AsyncMock()
        resp = await client.post(f"/api/projects/{pid}/start", headers={"X-User-Name": "alice"})
    assert resp.status_code == 202

    # Second start should fail (already scripting)
    with patch("app.api.pipeline.ArqRedis") as mock_arq:
        mock_arq.return_value.enqueue_job = AsyncMock()
        resp = await client.post(f"/api/projects/{pid}/start", headers={"X-User-Name": "alice"})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_patch_storyboard(client, db_session):
    from app.models.project import Project
    from sqlalchemy import select

    resp = await client.post(
        "/api/projects",
        json={"title": "T", "theme_text": "x"},
        headers={"X-User-Name": "alice"},
    )
    pid = resp.json()["id"]

    # Manually set status to script_review for testing
    result = await db_session.execute(select(Project).where(Project.id == pid))
    project = result.scalar_one()
    project.status = "script_review"
    await db_session.commit()

    resp = await client.patch(
        f"/api/projects/{pid}/storyboard",
        json={"scene_overview": "Updated overview"},
        headers={"X-User-Name": "alice"},
    )
    assert resp.status_code == 200
    assert resp.json()["scene_overview"] == "Updated overview"
```

- [ ] **Step 2: Run tests — expect failures**

```bash
cd backend
uv run pytest tests/integration/test_pipeline_api.py -v
```

Expected: Failures because pipeline.py is a stub.

- [ ] **Step 3: Implement pipeline.py**

Replace `backend/app/api/pipeline.py`:

```python
import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from arq.connections import ArqRedis, RedisSettings

from app.config import settings
from app.db import get_session
from app.main import get_redis
from app.models.project import Project, Shot, ReferenceImage
from app.models.schemas import (
    ProjectResponse, StoryboardPatch, ShotPatch, RegenerateShotsRequest
)
from app.services.state_machine import (
    ProjectStatus, ShotStatus, transition, InvalidTransitionError
)
from app.services.storage import archived_storyboard_path, storyboard_path

router = APIRouter()


def _require_user(x_user_name: str | None = Header(default=None)) -> str:
    if not x_user_name:
        raise HTTPException(status_code=400, detail="X-User-Name header required")
    return x_user_name


async def _get_project_or_404(project_id: str, session: AsyncSession) -> Project:
    result = await session.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def _get_arq(redis) -> ArqRedis:
    return ArqRedis(await redis.connection_pool)


@router.post("/projects/{project_id}/start", status_code=202)
async def start_project(
    project_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    project = await _get_project_or_404(project_id, session)

    # Validate at least one character image
    result = await session.execute(
        select(ReferenceImage).where(
            ReferenceImage.project_id == project_id,
            ReferenceImage.kind == "character",
        )
    )
    if not result.scalars().first():
        raise HTTPException(status_code=400, detail="At least one character reference image required")

    try:
        await transition(project, ProjectStatus.SCRIPTING, f"user:{user}", session, redis)
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    arq = await _get_arq(redis)
    await arq.enqueue_job("run_screenwriter", project_id, f"user:{user}")
    return {"status": "queued"}


@router.post("/projects/{project_id}/regenerate-script", status_code=202)
async def regenerate_script(
    project_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    project = await _get_project_or_404(project_id, session)

    # Archive current storyboard
    sb_path = storyboard_path(project_id)
    if sb_path.exists():
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        sb_path.rename(archived_storyboard_path(project_id, ts))

    # Clear shots
    result = await session.execute(select(Shot).where(Shot.project_id == project_id))
    for shot in result.scalars().all():
        await session.delete(shot)

    try:
        await transition(project, ProjectStatus.SCRIPTING, f"user:{user}", session, redis)
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    arq = await _get_arq(redis)
    await arq.enqueue_job("run_screenwriter", project_id, f"user:{user}")
    return {"status": "queued"}


@router.patch("/projects/{project_id}/storyboard", response_model=ProjectResponse)
async def patch_storyboard(
    project_id: str,
    body: StoryboardPatch,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    project = await _get_project_or_404(project_id, session)
    if project.status != ProjectStatus.SCRIPT_REVIEW.value:
        raise HTTPException(status_code=409, detail="Project must be in script_review status")

    if body.scene_overview is not None:
        project.scene_overview = body.scene_overview

    if body.shots is not None:
        result = await session.execute(select(Shot).where(Shot.project_id == project_id))
        shots_by_id = {s.shot_id: s for s in result.scalars().all()}
        for item in body.shots:
            shot = shots_by_id.get(item.shot_id)
            if shot:
                shot.text = item.text
                shot.shot_type = item.shot_type
                shot.visual_description = item.visual_description
                shot.shot_duration = item.shot_duration
                shot.align_with_previous = item.align_with_previous
                session.add(shot)

    project.updated_at = datetime.utcnow()
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


@router.post("/projects/{project_id}/approve-script", status_code=202)
async def approve_script(
    project_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    project = await _get_project_or_404(project_id, session)
    try:
        await transition(project, ProjectStatus.SHOT_GENERATING, f"user:{user}", session, redis)
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    # Reset PENDING on all shots for fresh run
    result = await session.execute(select(Shot).where(Shot.project_id == project_id))
    for shot in result.scalars().all():
        shot.status = ShotStatus.PENDING.value
        session.add(shot)
    await session.commit()

    arq = await _get_arq(redis)
    await arq.enqueue_job("run_shot_pipeline", project_id, f"user:{user}")
    return {"status": "queued"}


@router.post("/projects/{project_id}/regenerate-shots", status_code=202)
async def regenerate_shots(
    project_id: str,
    body: RegenerateShotsRequest,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    project = await _get_project_or_404(project_id, session)
    try:
        await transition(project, ProjectStatus.SHOT_GENERATING, f"user:{user}", session, redis)
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    result = await session.execute(
        select(Shot).where(
            Shot.project_id == project_id,
            Shot.shot_id.in_(body.shot_ids),
        )
    )
    for shot in result.scalars().all():
        shot.status = ShotStatus.PENDING.value
        shot.error_message = None
        session.add(shot)
    await session.commit()

    arq = await _get_arq(redis)
    await arq.enqueue_job("run_shot_pipeline", project_id, f"user:{user}")
    return {"status": "queued"}


@router.patch("/projects/{project_id}/shots/{shot_id}", response_model=dict)
async def patch_shot(
    project_id: str,
    shot_id: int,
    body: ShotPatch,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if shot is None:
        raise HTTPException(status_code=404, detail="Shot not found")

    if body.motion_prompt is not None:
        shot.motion_prompt = body.motion_prompt
    if body.align_with_previous is not None:
        shot.align_with_previous = body.align_with_previous
    shot.updated_at = datetime.utcnow()
    session.add(shot)
    await session.commit()
    await session.refresh(shot)
    return {"shot_id": shot.shot_id, "motion_prompt": shot.motion_prompt}


@router.post("/projects/{project_id}/export", status_code=202)
async def export_project(
    project_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    project = await _get_project_or_404(project_id, session)

    result = await session.execute(select(Shot).where(Shot.project_id == project_id))
    shots = result.scalars().all()
    if any(s.status != ShotStatus.COMPLETED.value for s in shots):
        raise HTTPException(status_code=400, detail="All shots must be COMPLETED before export")

    try:
        await transition(project, ProjectStatus.EXPORTING, f"user:{user}", session, redis)
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    arq = await _get_arq(redis)
    await arq.enqueue_job("run_merger", project_id, f"user:{user}")
    return {"status": "queued"}


@router.post("/projects/{project_id}/reset-to-script", status_code=202)
async def reset_to_script(
    project_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    project = await _get_project_or_404(project_id, session)

    sb_path = storyboard_path(project_id)
    if sb_path.exists():
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        sb_path.rename(archived_storyboard_path(project_id, ts))

    result = await session.execute(select(Shot).where(Shot.project_id == project_id))
    for shot in result.scalars().all():
        await session.delete(shot)

    try:
        await transition(project, ProjectStatus.SCRIPTING, f"user:{user}", session, redis)
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    arq = await _get_arq(redis)
    await arq.enqueue_job("run_screenwriter", project_id, f"user:{user}")
    return {"status": "queued"}


@router.post("/projects/{project_id}/reset", status_code=200)
async def reset_project(
    project_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    project = await _get_project_or_404(project_id, session)

    sb_path = storyboard_path(project_id)
    if sb_path.exists():
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        sb_path.rename(archived_storyboard_path(project_id, ts))

    result = await session.execute(select(Shot).where(Shot.project_id == project_id))
    for shot in result.scalars().all():
        await session.delete(shot)

    project.error_message = None
    try:
        await transition(project, ProjectStatus.DRAFT, f"user:{user}", session, redis)
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"status": "draft"}
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
cd backend
uv run pytest tests/integration/test_pipeline_api.py -v
```

Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd backend
git add app/api/pipeline.py tests/integration/test_pipeline_api.py
git commit -m "feat: pipeline API with state transitions and arq enqueue"
```

---

### Task 11: SSE Stream Endpoint

**Files:**
- Modify: `backend/app/api/stream.py`
- Create: `backend/tests/integration/test_sse.py`

- [ ] **Step 1: Write failing test**

Create `backend/tests/integration/test_sse.py`:

```python
import pytest
import asyncio


@pytest.mark.asyncio
async def test_sse_returns_snapshot_on_connect(client):
    resp = await client.post(
        "/api/projects",
        json={"title": "T", "theme_text": "x"},
        headers={"X-User-Name": "alice"},
    )
    pid = resp.json()["id"]

    # SSE connection should immediately yield state_snapshot
    async with client.stream("GET", f"/api/projects/{pid}/stream") as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        # Read first event
        lines = []
        async for line in response.aiter_lines():
            lines.append(line)
            if line == "":
                break  # end of first event

    event_lines = [l for l in lines if l.startswith("data:")]
    assert len(event_lines) == 1
    import json
    data = json.loads(event_lines[0].replace("data: ", ""))
    assert data["type"] == "state_snapshot"
    assert data["status"] == "draft"
```

- [ ] **Step 2: Run test — expect failure**

```bash
cd backend
uv run pytest tests/integration/test_sse.py -v
```

Expected: Failure — stream.py is a stub.

- [ ] **Step 3: Implement stream.py**

Replace `backend/app/api/stream.py`:

```python
import asyncio
import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.db import get_session
from app.main import get_redis
from app.models.project import Project, Shot
from app.services.events import subscribe

router = APIRouter()


@router.get("/projects/{project_id}/stream")
async def stream_events(
    project_id: str,
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    result = await session.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    shots_result = await session.execute(select(Shot).where(Shot.project_id == project_id))
    shots = shots_result.scalars().all()

    snapshot = {
        "type": "state_snapshot",
        "status": project.status,
        "storyboard": project.storyboard_path,
        "shots": [
            {
                "shot_id": s.shot_id,
                "status": s.status,
                "video_path": s.video_path,
            }
            for s in shots
        ],
    }

    async def event_generator():
        yield {"data": json.dumps(snapshot)}
        async for event in subscribe(redis, project_id):
            yield {"data": json.dumps(event)}

    return EventSourceResponse(event_generator())
```

- [ ] **Step 4: Run test — expect PASS**

```bash
cd backend
uv run pytest tests/integration/test_sse.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd backend
git add app/api/stream.py tests/integration/test_sse.py
git commit -m "feat: SSE stream endpoint with state snapshot on connect"
```

---

### Task 12: Worker Tasks

**Files:**
- Create: `backend/worker/tasks.py`
- Create: `backend/worker/arq_worker.py`

- [ ] **Step 1: Write tasks.py**

Create `backend/worker/tasks.py`:

```python
import json
import asyncio
from datetime import datetime
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.project import Project, Shot, ReferenceImage
from app.models.schemas import StoryboardShotItem
from app.services.state_machine import ProjectStatus, ShotStatus, transition
from app.services.storage import (
    storyboard_path, shot_dir, reference_images_dir, final_video_path
)
from app.services.events import publish
from app.agents.llm import GeminiProvider
from app.agents.screenwriter import run_screenwriter
from app.agents.director import run_director
from app.agents.video_generator import generate_video
from app.agents.frame_porter import extract_last_frame
from app.agents.merger import merge_shots


def _get_provider(ctx) -> GeminiProvider:
    return GeminiProvider(
        project=settings.gcp_project,
        location=settings.gcp_location,
        credentials_path=settings.google_application_credentials,
    )


def _prompts_dir() -> Path:
    return Path(__file__).parent.parent / "prompts"


async def run_screenwriter(ctx, project_id: str, actor: str) -> None:
    async with ctx["session_factory"]() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one()
        redis = ctx["redis"]

        # Load reference images
        ref_result = await session.execute(
            select(ReferenceImage).where(ReferenceImage.project_id == project_id)
            .order_by(ReferenceImage.kind, ReferenceImage.order_index)
        )
        ref_images = ref_result.scalars().all()
        image_data = []
        for img in ref_images:
            path = Path(img.storage_path)
            if path.exists():
                image_data.append((path.read_bytes(), f"{img.kind}: {img.filename}"))

        provider = _get_provider(ctx)
        retry_count = 0
        storyboard = None
        last_error = None

        while retry_count <= 2:
            try:
                storyboard = await run_screenwriter(
                    project, image_data, provider,
                    settings.gemini_script_model,
                    _prompts_dir() / "screenwriter.md",
                )
                break
            except Exception as e:
                last_error = e
                retry_count += 1
                if retry_count <= 2:
                    await asyncio.sleep(2 ** retry_count)

        if storyboard is None:
            project.error_message = str(last_error)
            session.add(project)
            await transition(project, ProjectStatus.FAILED, "system:worker", session, redis)
            return

        # Write storyboard.json
        sb_path = storyboard_path(project_id)
        sb_path.parent.mkdir(parents=True, exist_ok=True)
        sb_path.write_text(
            json.dumps({"scene_overview": storyboard.scene_overview,
                        "shots": [s.model_dump() for s in storyboard.shots]},
                       ensure_ascii=False),
            encoding="utf-8",
        )

        # Update project + insert shots in one transaction
        project.scene_overview = storyboard.scene_overview
        project.storyboard_path = str(sb_path)
        session.add(project)

        for item in storyboard.shots:
            shot = Shot(
                project_id=project_id,
                shot_id=item.shot_id,
                text=item.text,
                shot_type=item.shot_type,
                visual_description=item.visual_description,
                shot_duration=item.shot_duration,
                align_with_previous=item.align_with_previous,
                word_count_warning=getattr(item, "word_count_warning", False),
            )
            session.add(shot)

        await transition(project, ProjectStatus.SCRIPT_REVIEW, "system:worker", session, redis)

        await publish(redis, project_id, {
            "type": "script_ready",
            "storyboard": {"scene_overview": storyboard.scene_overview},
        })


async def run_shot_pipeline(ctx, project_id: str, actor: str) -> None:
    async with ctx["session_factory"]() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one()
        redis = ctx["redis"]

        shots_result = await session.execute(
            select(Shot)
            .where(Shot.project_id == project_id, Shot.status == ShotStatus.PENDING.value)
            .order_by(Shot.shot_id)
        )
        pending_shots = shots_result.scalars().all()

        provider = _get_provider(ctx)
        genai_client = provider.client

        has_failures = False

        for shot in pending_shots:
            await publish(redis, project_id, {"type": "shot_started", "shot_id": shot.shot_id})

            try:
                # Director
                shot.status = ShotStatus.PROMPT_GENERATING.value
                session.add(shot)
                await session.commit()

                motion_prompt = await run_director(
                    shot, provider, settings.gemini_director_model,
                    _prompts_dir() / "director.md",
                )
                shot.motion_prompt = motion_prompt

                # Pick first frame
                first_frame = _pick_first_frame(project, shot, session)
                shot.first_frame_path = str(first_frame)

                # Video generation
                shot.status = ShotStatus.VIDEO_GENERATING.value
                session.add(shot)
                await session.commit()
                await publish(redis, project_id, {
                    "type": "shot_progress",
                    "shot_id": shot.shot_id,
                    "sub_status": "video_generating",
                })

                s_dir = shot_dir(project_id, shot.shot_id)
                s_dir.mkdir(parents=True, exist_ok=True)
                video_out = s_dir / "output.mp4"

                video_bytes = await generate_video(
                    client=genai_client,
                    motion_prompt=motion_prompt,
                    first_frame_path=str(first_frame),
                    shot_duration=shot.shot_duration,
                    spoken_text=shot.text,
                    poll_interval=settings.veo_poll_interval_seconds,
                    max_wait=settings.veo_max_wait_seconds,
                )
                video_out.write_bytes(video_bytes)
                shot.video_path = str(video_out)

                # Extract last frame
                last_frame_out = s_dir / "last_frame.png"
                extract_last_frame(str(video_out), str(last_frame_out))
                shot.last_frame_path = str(last_frame_out)

                shot.status = ShotStatus.COMPLETED.value
                session.add(shot)
                await session.commit()

                await publish(redis, project_id, {
                    "type": "shot_completed",
                    "shot_id": shot.shot_id,
                    "video_path": str(video_out),
                })

            except Exception as e:
                shot.status = ShotStatus.FAILED.value
                shot.error_message = str(e)
                session.add(shot)
                await session.commit()
                has_failures = True
                await publish(redis, project_id, {
                    "type": "shot_failed",
                    "shot_id": shot.shot_id,
                    "error": str(e),
                })
                break  # stop processing remaining shots on failure

        await transition(project, ProjectStatus.SHOT_REVIEW, "system:worker", session, redis)
        await publish(redis, project_id, {
            "type": "all_shots_ready",
            "has_failures": has_failures,
        })


def _pick_first_frame(project, shot, session) -> Path:
    """Return path to first frame: character ref image or previous shot's last frame."""
    if shot.shot_id == 1 or not shot.align_with_previous:
        return _get_first_character_ref(project.id, session)
    # Try to get previous shot's last frame (synchronous lookup from already-loaded data)
    # This is called within an async context but uses already-fetched shot data
    prev_last = Path(project.id) / "shots" / f"shot_{shot.shot_id - 1}" / "last_frame.png"
    full = Path(settings.storage_root) / "projects" / str(prev_last)
    if full.exists():
        return full
    return _get_first_character_ref(project.id, session)


def _get_first_character_ref(project_id: str, session) -> Path:
    ref_dir = reference_images_dir(project_id)
    images = sorted(ref_dir.glob("*.jpg")) + sorted(ref_dir.glob("*.png")) + sorted(ref_dir.glob("*.jpeg"))
    if not images:
        raise ValueError("No character reference image found")
    return images[0]


async def run_merger(ctx, project_id: str, actor: str) -> None:
    async with ctx["session_factory"]() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one()
        redis = ctx["redis"]

        shots_result = await session.execute(
            select(Shot)
            .where(Shot.project_id == project_id, Shot.status == ShotStatus.COMPLETED.value)
            .order_by(Shot.shot_id)
        )
        shots = shots_result.scalars().all()
        shot_paths = [s.video_path for s in shots if s.video_path]

        final_path = final_video_path(project_id)
        final_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            merge_shots(shot_paths, str(final_path))
            project.final_video_path = str(final_path)
            session.add(project)
            await transition(project, ProjectStatus.EXPORTED, "system:worker", session, redis)
            await publish(redis, project_id, {
                "type": "export_done",
                "download_url": f"/api/projects/{project_id}/final.mp4",
            })
        except Exception as e:
            project.error_message = str(e)
            session.add(project)
            await transition(project, ProjectStatus.FAILED, "system:worker", session, redis)
            await publish(redis, project_id, {"type": "pipeline_failed", "reason": str(e)})
```

- [ ] **Step 2: Write arq_worker.py**

Create `backend/worker/arq_worker.py`:

```python
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import redis.asyncio as aioredis

from app.config import settings
from worker.tasks import run_screenwriter, run_shot_pipeline, run_merger


async def startup(ctx):
    ctx["redis"] = await aioredis.from_url(settings.redis_url, decode_responses=False)
    engine = create_async_engine(settings.database_url, echo=False)
    ctx["session_factory"] = async_sessionmaker(engine, expire_on_commit=False)
    ctx["engine"] = engine


async def shutdown(ctx):
    await ctx["redis"].aclose()
    await ctx["engine"].dispose()


class WorkerSettings:
    functions = [run_screenwriter, run_shot_pipeline, run_merger]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = settings.worker_pool_size
    job_timeout = 1800  # 30 minutes
    on_startup = startup
    on_shutdown = shutdown
```

- [ ] **Step 3: Verify imports parse without error**

```bash
cd backend
uv run python -c "from worker.arq_worker import WorkerSettings; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
cd backend
git add worker/tasks.py worker/arq_worker.py
git commit -m "feat: arq worker with screenwriter, shot pipeline, and merger tasks"
```

---

### Task 13: Prompt Files

**Files:**
- Create: `backend/prompts/screenwriter.md`
- Create: `backend/prompts/director.md`

- [ ] **Step 1: Copy existing prompt files from project root**

```bash
cp /home/wayne/tools/video_maker/screenwriter.md backend/prompts/screenwriter.md
cp /home/wayne/tools/video_maker/director.md backend/prompts/director.md
```

- [ ] **Step 2: Verify files exist and are non-empty**

```bash
wc -l backend/prompts/screenwriter.md backend/prompts/director.md
```

Expected: Both files have > 0 lines.

- [ ] **Step 3: Commit**

```bash
cd backend
git add prompts/
git commit -m "feat: add screenwriter and director system prompts"
```

---

### Task 14: Run Full Test Suite

- [ ] **Step 1: Run all unit tests**

```bash
cd backend
uv run pytest tests/unit/ -v
```

Expected: All tests PASS.

- [ ] **Step 2: Run all integration tests**

```bash
cd backend
uv run pytest tests/integration/ -v
```

Expected: All tests PASS.

- [ ] **Step 3: Run coverage check**

```bash
cd backend
uv run pytest --cov=app --cov-report=term-missing
```

Expected: Coverage ≥ 70% on `app/`.

- [ ] **Step 4: Final commit**

```bash
cd backend
git add -A
git commit -m "chore: verify full test suite passes with ≥70% coverage"
```

---

## Self-Review

**Spec coverage checklist:**

| Spec Section | Covered | Task |
|---|---|---|
| uv package management | ✅ | Task 1 |
| Directory structure | ✅ | Task 1 |
| Config / pydantic-settings | ✅ | Task 2 |
| SQLAlchemy async engine | ✅ | Task 2 |
| ORM: projects, shots, reference_images, events | ✅ | Task 3 |
| Pydantic schemas | ✅ | Task 4 |
| State machine + InvalidTransitionError | ✅ | Task 5 |
| Storage path helpers | ✅ | Task 6 |
| Redis pub/sub events | ✅ | Task 6 |
| GeminiProvider (generate_json / generate_text) | ✅ | Task 7 |
| Screenwriter agent + word count check | ✅ | Task 7 |
| Director agent + spoken text append | ✅ | Task 7 |
| VideoGenerator + Veo3 polling + timeout | ✅ | Task 7 |
| FramePorter (ffmpeg last frame) | ✅ | Task 7 |
| Merger (ffmpeg concat) | ✅ | Task 7 |
| GET/POST/GET/DELETE /api/projects | ✅ | Task 8 |
| Query filters (status, creator, limit, offset) | ✅ | Task 8 |
| Reference image upload + delete | ✅ | Task 9 |
| Asset serving + final.mp4 download | ✅ | Task 9 |
| POST /start (character image validation) | ✅ | Task 10 |
| POST /regenerate-script (archive storyboard) | ✅ | Task 10 |
| PATCH /storyboard | ✅ | Task 10 |
| POST /approve-script | ✅ | Task 10 |
| POST /regenerate-shots (body shot_ids) | ✅ | Task 10 |
| PATCH /shots/{shot_id} | ✅ | Task 10 |
| POST /export (all-COMPLETED check) | ✅ | Task 10 |
| POST /reset-to-script | ✅ | Task 10 |
| POST /reset (FAILED → DRAFT) | ✅ | Task 10 |
| SSE: state_snapshot on connect | ✅ | Task 11 |
| SSE: incremental Redis events | ✅ | Task 11 |
| arq worker: run_screenwriter + retry | ✅ | Task 12 |
| arq worker: run_shot_pipeline (serial) | ✅ | Task 12 |
| arq worker: run_merger | ✅ | Task 12 |
| WorkerSettings (max_jobs, job_timeout) | ✅ | Task 12 |
| pick_first_frame logic | ✅ | Task 12 |
| Soft failure: shot FAILED → SHOT_REVIEW | ✅ | Task 12 |
| Prompt files in backend/prompts/ | ✅ | Task 13 |
| Dockerfile | ✅ | Task 1 |
