# 内容分析 → 创作简报 → 生成（垂直切片）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 video_maker 后端加入「上传多条爆款视频 → 本地 ASR 转写口播 → 一次 LLM 联合归纳出 creation brief → brief 快照挂到 project、screenwriter 生成时注入」的垂直切片。

**Architecture:** 复用现有 `Project → Shot` 的架构范式：新增 `ContentAnalysis → ReferenceSample` 两张表；ARQ worker 跑 3 步状态机（`transcribing → analyzing → completed`）；进度经现有 redis pubsub / SSE 推送；ASR 用本地 faster-whisper-large-v3（`asr` 依赖组，仅 worker）；brief 归纳复用现有 `GeminiProvider`（Vertex）。只分析音频转写文本，不做结构化打标、不采集 caption、不做对照组。

**Tech Stack:** FastAPI · SQLAlchemy(async, sqlite) · ARQ · redis · pydantic v2 · faster-whisper · google-genai(Vertex) · pytest(asyncio_mode=auto)

**Spec:** `docs/superpowers/specs/2026-07-23-content-analysis-brief-slice-design.md`

## Global Constraints

- **本切片范围**：只交付**后端**垂直切片，全程可经 API + worker 测试。前端 UI（spec §10）与 Playwright UI e2e 为**后续计划**，不在本 plan。
- **Python 运行**：测试用 `uv run --project backend pytest`（**直接跑，不用 podman**）。绝不 `python`/`pip`。
- **Vertex 强制**：任何 `google.genai` 调用必须 `genai.Client(vertexai=True, ...)`，禁 API key。
- **ASR 本地免费**：faster-whisper 在 `backend/pyproject.toml` 的 `[dependency-groups].asr` 组（已存在），装：`uv sync --project backend --group asr`。仅 worker 需要。
- **唯一计费边界**：brief 归纳那次 LLM 调用。测试只在此边界打桩；ASR 可 stub「为提速」。**绝不伪造被测流程/数据**。
- **音频合规（NFR-2.1）**：抽出的 `audio.wav` 是中间产物，转写后**必须在 `finally` 删除**。
- **无硬编码绝对路径**：路径用 `Path(settings.storage_root)` / `Path(__file__)` 派生。
- **DB 迁移**：无 Alembic。新表由 `Base.metadata.create_all` 自动建；给现有 `projects` 表加列必须在 `backend/app/db.py::_run_migrations` 补 `_has_column` guard。
- **命名锁定**（跨 task 一致）：模型 `ContentAnalysis` / `ReferenceSample`；worker 任务名字符串 `"run_content_analysis"`；ASR `transcribe()→TranscriptResult`；brief pydantic 模型 `CreationBrief`；screenwriter 注入参数名 `creation_brief`；hook 阈值 `HOOK_CUTOFF_SEC = 3.0`。

---

## File Structure

**新建：**
- `backend/app/agents/asr.py` — faster-whisper 封装：`transcribe()` + 纯函数 `slice_hook()`
- `backend/app/agents/content_analyst.py` — `CreationBrief` schema + `build_user_parts()` + `run_content_analysis_brief()`
- `backend/app/api/content_analysis.py` — 新 router：建分析+上传、列表、详情、SSE、挂载 brief
- `backend/tests/unit/test_asr_hook.py`
- `backend/tests/unit/test_content_analyst.py`
- `backend/tests/unit/test_screenwriter_brief.py`
- `backend/tests/integration/test_content_analysis_worker.py`
- `backend/tests/integration/test_content_analysis_api.py`
- `backend/tests/integration/test_screenwriter_brief_wiring.py`

**修改：**
- `backend/app/models/project.py` — 加 `ContentAnalysis`/`ReferenceSample`/两个状态枚举 + `projects` 两列
- `backend/app/db.py` — `_run_migrations` 补两列 guard
- `backend/app/services/storage.py` — `analysis_dir()`/`sample_dir()`
- `backend/app/config.py` — `asr_model`/`asr_device`/`asr_compute_type`/`content_analysis_model`
- `backend/app/models/schemas.py` — `ReferenceSampleResponse`/`ContentAnalysisResponse`/`ContentAnalysisList`/`AttachBriefRequest`
- `backend/app/agents/screenwriter.py` — `render_brief_section()` + `run_screenwriter(..., creation_brief=None)`
- `backend/worker/tasks.py` — 新 `run_content_analysis()`；现有 `run_screenwriter` 任务读 `attached_brief_json` 传入
- `backend/worker/arq_worker.py` — 注册 `run_content_analysis`
- `backend/app/main.py` — 注册 content_analysis router
- `backend/tests/integration/conftest.py` — `client` fixture 加 content_analysis 模块的 `_get_arq_redis` patch

---

## Task 1: 数据模型（ContentAnalysis / ReferenceSample + projects 两列 + 迁移）

**Files:**
- Modify: `backend/app/models/project.py`（在 `ImageCandidate` 之后、`Event` 之前插入新模型；`Project` 类内加两列）
- Modify: `backend/app/db.py:120-130`（`_run_migrations` 末尾的 for 循环之外，追加 projects 列 guard）
- Test: `backend/tests/integration/test_content_analysis_worker.py`（本 task 只放一个建表/关系测试；worker 测试在 Task 7 追加）

**Interfaces:**
- Produces:
  - `ContentAnalysis(id:str, title:str, region_hint:Optional[str], status:str, brief_json:Optional[str], error_message:Optional[str], created_at, updated_at, samples:list)`
  - `ReferenceSample(id:int, analysis_id:str, order_index:int, video_path:str, audio_path:Optional[str], has_speech:Optional[bool], hook_text:Optional[str], full_transcript:Optional[str], language:Optional[str], status:str, error_message:Optional[str], created_at, analysis)`
  - `ContentAnalysisStatus`（`uploading|transcribing|analyzing|completed|failed`）、`ReferenceSampleStatus`（`pending|transcribing|transcribed|failed`）
  - `Project.content_analysis_id:Optional[str]`、`Project.attached_brief_json:Optional[str]`

- [ ] **Step 1: 写失败测试**

`backend/tests/integration/test_content_analysis_worker.py`：
```python
import pytest
from sqlalchemy import select
from app.models.project import (
    ContentAnalysis, ReferenceSample,
    ContentAnalysisStatus, ReferenceSampleStatus,
)


async def test_analysis_and_samples_persist(db_session_factory):
    async with db_session_factory() as s:
        a = ContentAnalysis(title="美妆赛道-账号A", region_hint="en")
        a.samples.append(ReferenceSample(order_index=0, video_path="/x/0.mp4"))
        a.samples.append(ReferenceSample(order_index=1, video_path="/x/1.mp4"))
        s.add(a)
        await s.commit()
        aid = a.id

    async with db_session_factory() as s:
        row = (await s.execute(
            select(ContentAnalysis).where(ContentAnalysis.id == aid)
        )).scalar_one()
        assert row.status == ContentAnalysisStatus.UPLOADING.value
        assert len(row.samples) == 2
        assert row.samples[0].status == ReferenceSampleStatus.PENDING.value
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest tests/integration/test_content_analysis_worker.py -v`
Expected: FAIL — `ImportError: cannot import name 'ContentAnalysis'`

- [ ] **Step 3: 加模型**

`backend/app/models/project.py`：在 `class ContentAnalysisStatus` 等已有枚举旁加两个枚举（放在 `ReferenceImageKind` 之后）：
```python
class ContentAnalysisStatus(str, Enum):
    UPLOADING = "uploading"
    TRANSCRIBING = "transcribing"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    FAILED = "failed"


class ReferenceSampleStatus(str, Enum):
    PENDING = "pending"
    TRANSCRIBING = "transcribing"
    TRANSCRIBED = "transcribed"
    FAILED = "failed"
```
在 `class ImageCandidate` 之后、`class Event` 之前插入两张表：
```python
class ContentAnalysis(Base):
    __tablename__ = "content_analyses"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(Text, nullable=False)
    region_hint = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default=ContentAnalysisStatus.UPLOADING.value)
    brief_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    samples = relationship(
        "ReferenceSample",
        back_populates="analysis",
        cascade="all, delete-orphan",
        order_by="ReferenceSample.order_index",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_content_analyses_status", "status"),
        Index("ix_content_analyses_created_at", "created_at"),
    )


class ReferenceSample(Base):
    __tablename__ = "reference_samples"

    id = Column(Integer, primary_key=True, autoincrement=True)
    analysis_id = Column(
        String(36),
        ForeignKey("content_analyses.id", ondelete="CASCADE"),
        nullable=False,
    )
    order_index = Column(Integer, nullable=False, default=0)
    video_path = Column(Text, nullable=False)
    audio_path = Column(Text, nullable=True)
    has_speech = Column(Boolean, nullable=True)
    hook_text = Column(Text, nullable=True)
    full_transcript = Column(Text, nullable=True)
    language = Column(String(10), nullable=True)
    status = Column(String(20), nullable=False, default=ReferenceSampleStatus.PENDING.value)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    analysis = relationship("ContentAnalysis", back_populates="samples")

    __table_args__ = (
        Index("ix_reference_samples_analysis", "analysis_id"),
    )
```
在 `class Project` 类体内（`auto_voice_calibrate` 行之后）加两列：
```python
    content_analysis_id = Column(String(36), nullable=True)  # 溯源：挂载的分析 id
    attached_brief_json = Column(Text, nullable=True)         # brief 快照
```

- [ ] **Step 4: 补 projects 迁移 guard**

`backend/app/db.py`：在 `_run_migrations` 内、最后那个 `for col, typ in [...] shots` 循环**之后**追加：
```python
    for col, typ in [
        ("content_analysis_id", "VARCHAR(36)"),
        ("attached_brief_json", "TEXT"),
    ]:
        if not await _has_column("projects", col):
            await conn.execute(sa.text(f"ALTER TABLE projects ADD COLUMN {col} {typ}"))
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run --project backend pytest tests/integration/test_content_analysis_worker.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/project.py backend/app/db.py backend/tests/integration/test_content_analysis_worker.py
git commit -m "feat(model): ContentAnalysis/ReferenceSample 表 + projects brief 挂钩列"
```

---

## Task 2: 存储路径 + 配置项

**Files:**
- Modify: `backend/app/services/storage.py`（追加 analysis 路径 builder）
- Modify: `backend/app/config.py`（`Settings` 类体加 4 个字段）
- Test: `backend/tests/unit/test_storage_analysis.py`

**Interfaces:**
- Produces:
  - `analysis_dir(analysis_id: str) -> Path`、`sample_dir(analysis_id: str, sample_id) -> Path`
  - `settings.asr_model:str`、`settings.asr_device:str`、`settings.asr_compute_type:str`、`settings.content_analysis_model:str`

- [ ] **Step 1: 写失败测试**

`backend/tests/unit/test_storage_analysis.py`：
```python
from pathlib import Path
from app.services.storage import analysis_dir, sample_dir
from app.config import settings


def test_analysis_paths():
    root = Path(settings.storage_root)
    assert analysis_dir("A1") == root / "analyses" / "A1"
    assert sample_dir("A1", 7) == root / "analyses" / "A1" / "samples" / "7"


def test_asr_config_defaults():
    assert settings.asr_model == "large-v3"
    assert settings.asr_device in ("cpu", "cuda")
    assert settings.content_analysis_model  # non-empty
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest tests/unit/test_storage_analysis.py -v`
Expected: FAIL — `ImportError: cannot import name 'analysis_dir'`

- [ ] **Step 3: 加 storage builder**

`backend/app/services/storage.py`（紧跟 `shot_candidates_dir` 之后）：
```python
def analysis_dir(analysis_id: str) -> Path:
    return Path(settings.storage_root) / "analyses" / analysis_id


def sample_dir(analysis_id: str, sample_id) -> Path:
    return analysis_dir(analysis_id) / "samples" / str(sample_id)
```

- [ ] **Step 4: 加配置项**

`backend/app/config.py`：在 `Settings` 类体内（`worker_pool_size` 附近）加：
```python
    # 内容分析（爆款归因）
    asr_model: str = "large-v3"
    asr_device: str = "cpu"          # cpu | cuda
    asr_compute_type: str = "int8"   # int8 | float16
    content_analysis_model: str = "gemini-2.5-pro"
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run --project backend pytest tests/unit/test_storage_analysis.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/storage.py backend/app/config.py backend/tests/unit/test_storage_analysis.py
git commit -m "feat(config): analysis 存储路径 + ASR/分析模型配置项"
```

---

## Task 3: 响应/请求 schemas

**Files:**
- Modify: `backend/app/models/schemas.py`（文件末尾追加）
- Test: `backend/tests/unit/test_content_analysis_schemas.py`

**Interfaces:**
- Produces（Pydantic v2，`class Config: from_attributes = True`）：
  - `ReferenceSampleResponse`、`ContentAnalysisResponse(samples: List[ReferenceSampleResponse], ...)`、`ContentAnalysisList(analyses: List[...], total:int)`、`AttachBriefRequest(analysis_id:str)`

- [ ] **Step 1: 写失败测试**

`backend/tests/unit/test_content_analysis_schemas.py`：
```python
from app.models.schemas import ContentAnalysisResponse, ReferenceSampleResponse


def test_response_from_attributes():
    class FakeSample:
        id = 3; analysis_id = "A1"; order_index = 0
        video_path = "/x/0.mp4"; has_speech = True
        hook_text = "wait for it"; full_transcript = "wait for it, here is why"
        language = "en"; status = "transcribed"; error_message = None
        from datetime import datetime; created_at = datetime.utcnow()

    class FakeAnalysis:
        id = "A1"; title = "t"; region_hint = "en"; status = "completed"
        brief_json = '{"niche_summary":"x"}'; error_message = None
        from datetime import datetime
        created_at = datetime.utcnow(); updated_at = datetime.utcnow()
        samples = [FakeSample()]

    r = ContentAnalysisResponse.model_validate(FakeAnalysis())
    assert r.id == "A1"
    assert r.samples[0].hook_text == "wait for it"
    assert r.brief_json == '{"niche_summary":"x"}'
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest tests/unit/test_content_analysis_schemas.py -v`
Expected: FAIL — `ImportError: cannot import name 'ContentAnalysisResponse'`

- [ ] **Step 3: 加 schemas**

`backend/app/models/schemas.py` 末尾追加（顶部已有 `from datetime import datetime`、`from typing import List, Optional`、`from pydantic import BaseModel, Field`）：
```python
class ReferenceSampleResponse(BaseModel):
    id: int
    analysis_id: str
    order_index: int
    video_path: str
    has_speech: Optional[bool] = None
    hook_text: Optional[str] = None
    full_transcript: Optional[str] = None
    language: Optional[str] = None
    status: str
    error_message: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ContentAnalysisResponse(BaseModel):
    id: str
    title: str
    region_hint: Optional[str] = None
    status: str
    brief_json: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    samples: List[ReferenceSampleResponse] = []

    class Config:
        from_attributes = True


class ContentAnalysisList(BaseModel):
    analyses: List[ContentAnalysisResponse]
    total: int


class AttachBriefRequest(BaseModel):
    analysis_id: str = Field(..., min_length=1)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --project backend pytest tests/unit/test_content_analysis_schemas.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/schemas.py backend/tests/unit/test_content_analysis_schemas.py
git commit -m "feat(schema): ContentAnalysis/ReferenceSample 响应与请求模型"
```

---

## Task 4: ASR 模块（faster-whisper 封装）

先装依赖组：`uv sync --project backend --group asr`

**Files:**
- Create: `backend/app/agents/asr.py`
- Test: `backend/tests/unit/test_asr_hook.py`

**Interfaces:**
- Produces:
  - `HOOK_CUTOFF_SEC = 3.0`
  - `@dataclass TranscriptResult(has_speech: bool, full_transcript: str, hook_text: str, language: Optional[str])`
  - `slice_hook(words, cutoff: float = HOOK_CUTOFF_SEC) -> str` — `words` 为含 `.start`(float|None) 与 `.word`(str) 的对象序列
  - `transcribe(audio_path: str, language: Optional[str] = None) -> TranscriptResult`
- Consumes: `settings.asr_model/asr_device/asr_compute_type`

- [ ] **Step 1: 写失败测试**（只测纯逻辑 `slice_hook`，不加载模型）

`backend/tests/unit/test_asr_hook.py`：
```python
from collections import namedtuple
from app.agents.asr import slice_hook, HOOK_CUTOFF_SEC

W = namedtuple("W", ["start", "word"])


def test_slice_hook_keeps_words_before_cutoff():
    words = [W(0.0, "wait"), W(1.2, "for"), W(2.9, "it"), W(3.4, "because"), W(5.0, "reasons")]
    assert slice_hook(words) == "wait for it"


def test_slice_hook_handles_none_start_and_whitespace():
    words = [W(None, "x"), W(0.5, "  hi "), W(2.0, "there")]
    assert slice_hook(words) == "hi there"


def test_cutoff_is_three_seconds():
    assert HOOK_CUTOFF_SEC == 3.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest tests/unit/test_asr_hook.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agents.asr'`

- [ ] **Step 3: 写实现**

`backend/app/agents/asr.py`：
```python
"""本地 ASR：faster-whisper-large-v3，内置 Silero VAD + 词级时间戳。"""

import logging
from dataclasses import dataclass
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

HOOK_CUTOFF_SEC = 3.0


@dataclass
class TranscriptResult:
    has_speech: bool
    full_transcript: str
    hook_text: str
    language: Optional[str]


def slice_hook(words, cutoff: float = HOOK_CUTOFF_SEC) -> str:
    """取所有 start < cutoff 的词，拼成 hook_text（母 FRD FR-3.5）。"""
    kept = [
        w.word.strip()
        for w in words
        if getattr(w, "start", None) is not None and w.start < cutoff
    ]
    return " ".join(t for t in kept if t).strip()


_model = None


def _get_model():
    """进程内单例，避免逐样本重载 ~3GB 权重。"""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel  # 延迟导入：仅 worker 装 asr 组
        logger.info("loading faster-whisper model=%s device=%s compute=%s",
                    settings.asr_model, settings.asr_device, settings.asr_compute_type)
        _model = WhisperModel(
            settings.asr_model,
            device=settings.asr_device,
            compute_type=settings.asr_compute_type,
        )
    return _model


def transcribe(audio_path: str, language: Optional[str] = None) -> TranscriptResult:
    """VAD 过滤 + 词级时间戳；无有效语音段 → has_speech=False。"""
    model = _get_model()
    segments, info = model.transcribe(
        audio_path,
        vad_filter=True,
        word_timestamps=True,
        language=language,
    )
    segments = list(segments)  # generator → list
    if not segments:
        return TranscriptResult(False, "", "", getattr(info, "language", None))

    words = [w for seg in segments for w in (seg.words or [])]
    full = " ".join(seg.text.strip() for seg in segments).strip()
    return TranscriptResult(
        has_speech=bool(full),
        full_transcript=full,
        hook_text=slice_hook(words),
        language=getattr(info, "language", None),
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --project backend pytest tests/unit/test_asr_hook.py -v`
Expected: PASS（不触发模型加载）

- [ ] **Step 5: Commit**

```bash
git add backend/app/agents/asr.py backend/tests/unit/test_asr_hook.py
git commit -m "feat(asr): faster-whisper 封装 + hook 词级切分（前3秒）"
```

---

## Task 5: 内容分析 brief 模块

**Files:**
- Create: `backend/app/agents/content_analyst.py`
- Test: `backend/tests/unit/test_content_analyst.py`

**Interfaces:**
- Produces:
  - pydantic `CreationBrief`（含 `SampleStats`/`HookStrategy`/`ScriptStructure` 嵌套）
  - `build_user_parts(samples: List[dict]) -> List[dict]` — `samples` 每项 `{"hook_text":str,"full_transcript":str}`
  - `async run_content_analysis_brief(samples: List[dict], provider, model: str) -> dict`
- Consumes: `GeminiProvider.generate_json(model, system_prompt, user_parts, response_schema, temperature, operation)`（Task 5 用 stub provider，Task 7 用真 provider）

- [ ] **Step 1: 写失败测试**

`backend/tests/unit/test_content_analyst.py`：
```python
import pytest
from app.agents.content_analyst import (
    build_user_parts, run_content_analysis_brief, CreationBrief,
)

SAMPLES = [
    {"hook_text": "wait for it", "full_transcript": "wait for it, here is the trick"},
    {"hook_text": "3 mistakes", "full_transcript": "3 mistakes you make daily"},
]


def test_build_user_parts_includes_transcripts():
    parts = build_user_parts(SAMPLES)
    blob = " ".join(p["data"] for p in parts if p["type"] == "text")
    assert "wait for it" in blob
    assert "3 mistakes you make daily" in blob
    assert all(p["type"] == "text" for p in parts)  # 无 caption/图像


class _StubProvider:
    def __init__(self, canned):
        self.canned = canned
        self.seen = None

    async def generate_json(self, *, model, system_prompt, user_parts,
                            response_schema, temperature=0.7, operation=None):
        self.seen = {"model": model, "user_parts": user_parts, "operation": operation}
        return self.canned


CANNED = {
    "niche_summary": "美妆快节奏教程",
    "sample_stats": {"sample_n": 2, "no_speech_pct": 0.0, "sample_warning": None},
    "hook_strategy": {"common_hook_types": ["悬念"], "example_hooks": ["wait for it"]},
    "script_structure": {"pacing": "快", "emotion": "正向", "info_gap": "制造", "cta": "引导评论"},
    "do": ["前3秒抛悬念"], "dont": ["平铺直叙"],
    "screenwriter_directives": "开场0秒抛出悬念钩子，语速偏快。",
}


async def test_run_brief_returns_validated_dict():
    prov = _StubProvider(CANNED)
    out = await run_content_analysis_brief(SAMPLES, prov, "gemini-2.5-pro")
    CreationBrief.model_validate(out)              # schema 合法
    assert out["screenwriter_directives"].startswith("开场")
    assert prov.seen["model"] == "gemini-2.5-pro"
    assert prov.seen["operation"] == "agents-content-analyst-brief"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest tests/unit/test_content_analyst.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agents.content_analyst'`

- [ ] **Step 3: 写实现**

`backend/app/agents/content_analyst.py`：
```python
"""跨爆款样本的转写文本联合归纳 → creation brief（无对照、无结构化打标）。"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class SampleStats(BaseModel):
    sample_n: int
    no_speech_pct: float
    sample_warning: Optional[str] = None


class HookStrategy(BaseModel):
    common_hook_types: List[str]
    example_hooks: List[str]


class ScriptStructure(BaseModel):
    pacing: str
    emotion: str
    info_gap: str
    cta: str


class CreationBrief(BaseModel):
    niche_summary: str
    sample_stats: SampleStats
    hook_strategy: HookStrategy
    script_structure: ScriptStructure
    do: List[str]
    dont: List[str]
    screenwriter_directives: str


SYSTEM_PROMPT = """你是短视频爆款内容分析师。下面是若干条「爆款视频」的口播转写文本（含前3秒钩子 hook）。
请通读所有样本，联合归纳它们的共性制胜模式，输出一份可直接指导创作的简报。

要求：
- 只依据给出的口播文本归纳，不要臆造数据。
- 每条结论面向「怎么写下一条」，可执行。
- screenwriter_directives 写成一段可直接下发给编剧的中文创作指令。
- 严格输出 schema 指定的纯 JSON，无任何解释性文字。
注意：sample_stats 由系统另行填充，你填的 sample_stats 会被覆盖，可填占位值。"""


def build_user_parts(samples: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    parts: List[Dict[str, Any]] = []
    for i, s in enumerate(samples, 1):
        hook = (s.get("hook_text") or "").strip()
        full = (s.get("full_transcript") or "").strip()
        parts.append({
            "type": "text",
            "data": f"样本 {i}\n【前3秒钩子】{hook}\n【完整口播】{full}",
        })
    return parts


async def run_content_analysis_brief(
    samples: List[Dict[str, str]], provider, model: str
) -> Dict[str, Any]:
    return await provider.generate_json(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        user_parts=build_user_parts(samples),
        response_schema=CreationBrief,
        temperature=0.4,
        operation="agents-content-analyst-brief",
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --project backend pytest tests/unit/test_content_analyst.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/agents/content_analyst.py backend/tests/unit/test_content_analyst.py
git commit -m "feat(analyst): 转写文本联合归纳 CreationBrief"
```

---

## Task 6: Screenwriter brief 注入 + worker 传参

**Files:**
- Modify: `backend/app/agents/screenwriter.py`（加 `render_brief_section`；`run_screenwriter` 加参 `creation_brief`）
- Modify: `backend/worker/tasks.py`（现有 `run_screenwriter` worker 任务：读 `project.attached_brief_json` 传入）
- Test: `backend/tests/unit/test_screenwriter_brief.py`、`backend/tests/integration/test_screenwriter_brief_wiring.py`

**Interfaces:**
- Consumes: `CreationBrief` 的 dict 形态（`screenwriter_directives`/`hook_strategy`/`script_structure`）
- Produces: `render_brief_section(brief: dict) -> str`；`run_screenwriter(theme_text, reference_images, llm_provider, aspect_ratio="16:9", creation_brief: Optional[dict]=None)`

- [ ] **Step 1: 写失败单元测试**

`backend/tests/unit/test_screenwriter_brief.py`：
```python
import pytest
from app.agents.screenwriter import render_brief_section, run_screenwriter

BRIEF = {
    "niche_summary": "美妆快节奏教程",
    "hook_strategy": {"common_hook_types": ["悬念"], "example_hooks": ["wait for it"]},
    "script_structure": {"pacing": "快", "emotion": "正向", "info_gap": "制造", "cta": "引导评论"},
    "screenwriter_directives": "开场0秒抛悬念钩子，语速偏快。",
}


def test_render_brief_section_contains_directives_and_hooks():
    text = render_brief_section(BRIEF)
    assert "开场0秒抛悬念钩子" in text
    assert "悬念" in text          # hook 类型
    assert "引导评论" in text      # cta


def test_render_brief_section_empty_when_none():
    assert render_brief_section(None) == ""


class _CaptureProvider:
    async def generate_json(self, *, model, system_prompt, user_parts,
                            response_schema, temperature=0.7, operation=None):
        self.captured = " ".join(p["data"] for p in user_parts if p["type"] == "text")
        return {"scene_overview": "ov", "shots": [
            {"shot_id": 1, "text": "hi there friend", "shot_type": "Close-up",
             "visual_description": "d", "shot_duration": 4, "align_with_previous": False}
        ]}


async def test_run_screenwriter_injects_brief_into_prompt():
    prov = _CaptureProvider()
    await run_screenwriter("主题X", [], prov, "9:16", creation_brief=BRIEF)
    assert "开场0秒抛悬念钩子" in prov.captured
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest tests/unit/test_screenwriter_brief.py -v`
Expected: FAIL — `ImportError: cannot import name 'render_brief_section'`

- [ ] **Step 3: 改 screenwriter.py**

`backend/app/agents/screenwriter.py`：加渲染函数（放在 `run_screenwriter` 之前）：
```python
def render_brief_section(brief: Optional[Dict[str, Any]]) -> str:
    """把 creation brief 渲染成一段可注入 screenwriter 的中文指令；None → 空串。"""
    if not brief:
        return ""
    hook = brief.get("hook_strategy", {}) or {}
    struct = brief.get("script_structure", {}) or {}
    lines = [
        "【赛道爆款简报 — 务必据此创作】",
        brief.get("screenwriter_directives", "").strip(),
        f"常见钩子类型：{'、'.join(hook.get('common_hook_types', []))}",
        f"节奏/情绪：{struct.get('pacing', '')} / {struct.get('emotion', '')}；"
        f"信息缺口：{struct.get('info_gap', '')}；CTA：{struct.get('cta', '')}",
    ]
    return "\n".join(x for x in lines if x)
```
`run_screenwriter` 签名加参数：
```python
async def run_screenwriter(
    theme_text: str,
    reference_images: List[Dict[str, Any]],
    llm_provider: GeminiProvider,
    aspect_ratio: str = "16:9",
    creation_brief: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
```
在函数体内、`user_parts.append({"type": "text", "data": f"主题：{theme_text}"})` 之前插入：
```python
    brief_text = render_brief_section(creation_brief)
    if brief_text:
        user_parts.append({"type": "text", "data": brief_text})
```

- [ ] **Step 4: 跑单元测试确认通过**

Run: `uv run --project backend pytest tests/unit/test_screenwriter_brief.py -v`
Expected: PASS

- [ ] **Step 5: 写 worker 传参集成测试（真实断言，先失败）**

> 前置约定：worker 的 `run_screenwriter` **任务**与 agent 函数同名会冲突，因此 Step 6 把 agent 以别名 `run_screenwriter_agent` 导入 —— 下面的测试就 patch 这个别名。

`backend/tests/integration/test_screenwriter_brief_wiring.py`：
```python
import json
import pytest
from sqlalchemy import select
from app.models.project import Project, ProjectStatus


class _FakeRedis:
    async def publish(self, *a, **k): return 0


async def test_worker_run_screenwriter_passes_attached_brief(db_session_factory, monkeypatch):
    """worker 的 run_screenwriter 任务应把 project.attached_brief_json 解析后
    作为 creation_brief 传给 agent。"""
    from worker import tasks as tasks_module

    captured = {}

    async def fake_agent(theme_text, reference_images, llm_provider,
                         aspect_ratio="16:9", creation_brief=None):
        captured["brief"] = creation_brief
        return {"storyboard": {"scene_overview": "o", "shots": []},
                "word_count_warnings": []}

    monkeypatch.setattr(tasks_module, "run_screenwriter_agent", fake_agent)

    async def _noop_publish(*a, **k): return True
    monkeypatch.setattr(tasks_module, "publish_event", _noop_publish, raising=False)

    async with db_session_factory() as s:
        p = Project(title="t", theme_text="主题X", creator_name="u",
                    status=ProjectStatus.SCRIPTING.value, aspect_ratio="9:16",
                    attached_brief_json=json.dumps({"screenwriter_directives": "开场抛悬念"}))
        s.add(p); await s.commit(); pid = p.id

    ctx = {"session_factory": db_session_factory, "redis": _FakeRedis()}
    await tasks_module.run_screenwriter(ctx, pid, "user:test")

    assert captured["brief"] == {"screenwriter_directives": "开场抛悬念"}
```
> 若 `run_screenwriter` 任务还依赖其它外部调用（取 reference images、构造 `GeminiProvider` 等），在测试里按其真实实现补**最小** stub —— 只 stub 外部/计费边界，**不伪造被测的 brief 传递逻辑本身**。

- [ ] **Step 6: 跑测试确认失败**

Run: `uv run --project backend pytest tests/integration/test_screenwriter_brief_wiring.py -v`
Expected: FAIL — `AttributeError: <module 'worker.tasks'> has no attribute 'run_screenwriter_agent'`

- [ ] **Step 7: 改 worker tasks.py 传入 brief**

`backend/worker/tasks.py`：
1. 顶部把 agent 函数改为别名导入（若原为 `from app.agents.screenwriter import run_screenwriter`）：
```python
from app.agents.screenwriter import run_screenwriter as run_screenwriter_agent
```
并把该任务体内对 agent 的调用相应改名为 `run_screenwriter_agent(...)`。
2. 在 worker 任务 `run_screenwriter(ctx, project_id, actor, ...)` 内，加载 `project` 之后、调用 agent 之前，插入：
```python
        import json
        creation_brief = (
            json.loads(project.attached_brief_json)
            if project.attached_brief_json else None
        )
```
3. 给 agent 调用加实参 `creation_brief=creation_brief`：
```python
        result = await run_screenwriter_agent(
            theme_text=project.theme_text,
            reference_images=ref_images,
            llm_provider=provider,
            aspect_ratio=project.aspect_ratio,
            creation_brief=creation_brief,
        )
```
> 若现有调用用位置参数/不同变量名，保持其余不变，仅新增 `creation_brief=` 关键字实参。

- [ ] **Step 8: 跑测试确认通过**

Run: `uv run --project backend pytest tests/unit/test_screenwriter_brief.py tests/integration/test_screenwriter_brief_wiring.py -v`
Expected: PASS（单元 3 个 + 集成 1 个）

- [ ] **Step 9: Commit**

```bash
git add backend/app/agents/screenwriter.py backend/worker/tasks.py backend/tests/unit/test_screenwriter_brief.py backend/tests/integration/test_screenwriter_brief_wiring.py
git commit -m "feat(screenwriter): 注入 creation brief + worker 从 attached_brief_json 传参"
```

---

## Task 7: Worker 分析任务 `run_content_analysis` + 注册

**Files:**
- Modify: `backend/worker/tasks.py`（加 `run_content_analysis`）
- Modify: `backend/worker/arq_worker.py`（import + `functions` 注册）
- Test: `backend/tests/integration/test_content_analysis_worker.py`（追加流程测试）

**Interfaces:**
- Produces: `async run_content_analysis(ctx, analysis_id: str, actor: str) -> None`；arq 任务名 `"run_content_analysis"`
- Consumes: `asr.transcribe`、`extract_audio_wav`、`run_content_analysis_brief`、`GeminiProvider`、`publish_event`/`create_event`、`sample_dir`

- [ ] **Step 1: 写失败流程测试**（stub ASR + stub brief provider；真 DB / 真状态机）

在 `backend/tests/integration/test_content_analysis_worker.py` 追加：
```python
import json
from app.agents import asr as asr_module
from app.agents.asr import TranscriptResult
from worker import tasks as tasks_module


class _FakeRedis:
    async def publish(self, *a, **k): return 0


async def _seed(db_session_factory, n_speech=3, n_silent=1):
    async with db_session_factory() as s:
        a = ContentAnalysis(title="t", region_hint="en")
        for i in range(n_speech + n_silent):
            a.samples.append(ReferenceSample(order_index=i, video_path=f"/x/{i}.mp4"))
        s.add(a); await s.commit()
        return a.id, [smp.id for smp in a.samples], n_speech


async def test_run_content_analysis_happy_path(db_session_factory, monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))

    aid, sample_ids, n_speech = await _seed(db_session_factory, 3, 1)

    # stub ffmpeg 抽音频：建个空 wav 占位
    def fake_extract(video_path, out_wav):
        from pathlib import Path
        Path(out_wav).parent.mkdir(parents=True, exist_ok=True)
        Path(out_wav).write_bytes(b"RIFF")
        return out_wav
    monkeypatch.setattr(tasks_module, "extract_audio_wav", fake_extract)

    # stub ASR：前 n_speech 条有语音，最后一条无语音
    calls = {"n": 0}
    def fake_transcribe(path, language=None):
        i = calls["n"]; calls["n"] += 1
        if i < n_speech:
            return TranscriptResult(True, f"transcript {i}", f"hook {i}", "en")
        return TranscriptResult(False, "", "", "en")
    monkeypatch.setattr(tasks_module.asr, "transcribe", fake_transcribe)

    # stub 计费边界：brief LLM
    async def fake_brief(samples, provider, model):
        return {
            "niche_summary": "x",
            "sample_stats": {"sample_n": 999, "no_speech_pct": 9.9, "sample_warning": "STALE"},
            "hook_strategy": {"common_hook_types": ["悬念"], "example_hooks": ["hook 0"]},
            "script_structure": {"pacing": "快", "emotion": "正向", "info_gap": "有", "cta": "评论"},
            "do": ["a"], "dont": ["b"], "screenwriter_directives": "开场抛悬念",
        }
    monkeypatch.setattr(tasks_module, "run_content_analysis_brief", fake_brief)
    # 避免真建 Vertex provider
    monkeypatch.setattr(tasks_module, "GeminiProvider", lambda **k: object())

    ctx = {"session_factory": db_session_factory, "redis": _FakeRedis()}
    await tasks_module.run_content_analysis(ctx, aid, "user:test")

    async with db_session_factory() as s:
        row = (await s.execute(
            select(ContentAnalysis).where(ContentAnalysis.id == aid))).scalar_one()
        assert row.status == ContentAnalysisStatus.COMPLETED.value
        brief = json.loads(row.brief_json)
        # 代码计算的 stats 覆盖 LLM 返回的占位
        assert brief["sample_stats"]["sample_n"] == 3
        assert brief["sample_stats"]["no_speech_pct"] == 0.25
        assert brief["sample_stats"]["sample_warning"] is None
        # 无人声样本被标记、不参与
        silent = [smp for smp in row.samples if smp.has_speech is False]
        assert len(silent) == 1
        assert all(smp.audio_path in (None, "") or not __import__("os").path.exists(smp.audio_path)
                   for smp in row.samples)  # wav 已删


async def test_run_content_analysis_all_silent_fails(db_session_factory, monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    aid, _, _ = await _seed(db_session_factory, 0, 2)
    monkeypatch.setattr(tasks_module, "extract_audio_wav",
                        lambda v, o: (__import__("pathlib").Path(o).parent.mkdir(parents=True, exist_ok=True)
                                      or __import__("pathlib").Path(o).write_bytes(b"x") or o))
    monkeypatch.setattr(tasks_module.asr, "transcribe",
                        lambda p, language=None: TranscriptResult(False, "", "", "en"))
    ctx = {"session_factory": db_session_factory, "redis": _FakeRedis()}
    await tasks_module.run_content_analysis(ctx, aid, "user:test")
    async with db_session_factory() as s:
        row = (await s.execute(
            select(ContentAnalysis).where(ContentAnalysis.id == aid))).scalar_one()
        assert row.status == ContentAnalysisStatus.FAILED.value
        assert row.error_message
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest tests/integration/test_content_analysis_worker.py -v`
Expected: FAIL — `AttributeError: module 'worker.tasks' has no attribute 'run_content_analysis'`

- [ ] **Step 3: 写实现**

`backend/worker/tasks.py`：顶部补导入（与现有导入风格一致）：
```python
import asyncio
import json
import os
from app.agents import asr
from app.agents.audio_extractor import extract_audio_wav
from app.agents.content_analyst import run_content_analysis_brief
from app.agents.llm import GeminiProvider
from app.services.storage import sample_dir
from app.services.events import publish_event, create_event
from app.models.project import (
    ContentAnalysis, ReferenceSample,
    ContentAnalysisStatus, ReferenceSampleStatus,
)
```
加任务函数：
```python
async def run_content_analysis(ctx, analysis_id: str, actor: str) -> None:
    wc = WorkerContext(ctx)
    session_factory = wc.session_factory
    redis = wc.redis

    async def _publish(session, analysis):
        await publish_event(redis, analysis_id, create_event(
            "analysis_progress", status=analysis.status, analysis_id=analysis_id))

    async with session_factory() as session:
        analysis = (await session.execute(
            select(ContentAnalysis).where(ContentAnalysis.id == analysis_id)
        )).scalar_one_or_none()
        if analysis is None:
            return
        samples = (await session.execute(
            select(ReferenceSample).where(ReferenceSample.analysis_id == analysis_id)
            .order_by(ReferenceSample.order_index)
        )).scalars().all()

        # --- transcribing ---
        analysis.status = ContentAnalysisStatus.TRANSCRIBING.value
        await session.commit(); await _publish(session, analysis)

        for smp in samples:
            smp.status = ReferenceSampleStatus.TRANSCRIBING.value
            await session.commit(); await _publish(session, analysis)
            wav = sample_dir(analysis_id, smp.id) / "audio.wav"
            try:
                await asyncio.to_thread(extract_audio_wav, smp.video_path, str(wav))
                res = await asyncio.to_thread(
                    asr.transcribe, str(wav), analysis.region_hint or None)
                smp.has_speech = res.has_speech
                smp.full_transcript = res.full_transcript
                smp.hook_text = res.hook_text
                smp.language = res.language
                smp.status = ReferenceSampleStatus.TRANSCRIBED.value
            except Exception as e:  # noqa: BLE001 — 逐样本失败不拖垮整体（FR-1.5）
                smp.status = ReferenceSampleStatus.FAILED.value
                smp.error_message = str(e)
            finally:
                if wav.exists():
                    os.remove(wav)  # NFR-2.1：中间产物转写后即删
                smp.audio_path = None
            await session.commit(); await _publish(session, analysis)

        # --- analyzing ---
        analyzable = [s for s in samples
                      if s.status == ReferenceSampleStatus.TRANSCRIBED.value and s.has_speech]
        if not analyzable:
            analysis.status = ContentAnalysisStatus.FAILED.value
            analysis.error_message = "无可用样本（全部无人声或转写失败）"
            await session.commit(); await _publish(session, analysis)
            return

        analysis.status = ContentAnalysisStatus.ANALYZING.value
        await session.commit(); await _publish(session, analysis)

        total = len(samples)
        no_speech = len([s for s in samples if s.has_speech is False])
        stats = {
            "sample_n": len(analyzable),
            "no_speech_pct": round(no_speech / total, 3) if total else 0.0,
            "sample_warning": "样本偏少，仅供参考" if len(analyzable) < 3 else None,
        }
        payload = [{"hook_text": s.hook_text or "", "full_transcript": s.full_transcript or ""}
                   for s in analyzable]
        provider = GeminiProvider(project=settings.gemini_project,
                                  location=settings.gemini_location)
        try:
            brief = await run_content_analysis_brief(
                payload, provider, settings.content_analysis_model)
            brief["sample_stats"] = stats  # 代码计算的 stats 为准，覆盖 LLM
            analysis.brief_json = json.dumps(brief, ensure_ascii=False)
            analysis.status = ContentAnalysisStatus.COMPLETED.value
        except Exception as e:  # noqa: BLE001
            analysis.status = ContentAnalysisStatus.FAILED.value
            analysis.error_message = f"brief 生成失败: {e}"
        await session.commit(); await _publish(session, analysis)
```
> `WorkerContext`、`select`、`settings` 已在 tasks.py 顶部存在（沿用 `run_shot_pipeline` 的导入）；若某个未导入则按需补。

`backend/worker/arq_worker.py`：import 行加入 `run_content_analysis`，并加进 `functions`：
```python
from worker.tasks import (
    run_screenwriter, run_shot_pipeline, run_merger,
    run_character_calibrate, run_character_calibrate_batch, run_image_candidate,
    run_content_analysis,
)
...
    functions = [
        run_screenwriter, run_shot_pipeline, run_merger,
        run_character_calibrate, run_character_calibrate_batch, run_image_candidate,
        run_content_analysis,
    ]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --project backend pytest tests/integration/test_content_analysis_worker.py -v`
Expected: PASS（3 个测试：建表关系 + happy path + 全无人声失败）

- [ ] **Step 5: Commit**

```bash
git add backend/worker/tasks.py backend/worker/arq_worker.py backend/tests/integration/test_content_analysis_worker.py
git commit -m "feat(worker): run_content_analysis 3步管线（转写→归纳→brief），逐样本容错+音频即删"
```

---

## Task 8: API router（建分析/上传、列表、详情、SSE、挂载 brief）+ 注册

**Files:**
- Create: `backend/app/api/content_analysis.py`
- Modify: `backend/app/main.py:124,133`（import + `include_router`）
- Modify: `backend/tests/integration/conftest.py`（`client` fixture 加本模块 `_get_arq_redis` patch）
- Test: `backend/tests/integration/test_content_analysis_api.py`

**Interfaces:**
- Produces（均挂 `/api` 前缀）：
  - `POST /analyses`（multipart：`title:Form`, `region_hint:Form(optional)`, `files:List[UploadFile]`）→ `ContentAnalysisResponse` 201，建 analysis+samples、`enqueue_job("run_content_analysis", analysis_id, "user:{user}")`
  - `GET /analyses` → `ContentAnalysisList`
  - `GET /analyses/{analysis_id}` → `ContentAnalysisResponse`
  - `GET /analyses/{analysis_id}/stream` → SSE（复用 `subscribe_to_events(redis, analysis_id)`）
  - `POST /projects/{project_id}/attach-brief`（body `AttachBriefRequest`）→ `ProjectResponse`，把分析 `brief_json` 快照进 `project.attached_brief_json` + `content_analysis_id`
  - 模块内定义 `async def _get_arq_redis(redis) -> ArqRedis`（供测试按模块 patch）

- [ ] **Step 1: 写失败测试**

`backend/tests/integration/test_content_analysis_api.py`：
```python
import json
import pytest
from sqlalchemy import select
from app.models.project import ContentAnalysis, Project, ProjectStatus

HEADERS = {"X-User-Name": "test-user"}


async def test_create_analysis_uploads_and_enqueues(client, db_session_factory):
    files = [
        ("files", ("a.mp4", b"fake-bytes-a", "video/mp4")),
        ("files", ("b.mp4", b"fake-bytes-b", "video/mp4")),
    ]
    r = await client.post("/api/analyses",
                          data={"title": "美妆A", "region_hint": "en"},
                          files=files, headers=HEADERS)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["title"] == "美妆A"
    assert len(body["samples"]) == 2
    assert body["status"] == "uploading"
    # 真入队（arq 被 fixture mock）
    client.arq.enqueue_job.assert_awaited()
    args = client.arq.enqueue_job.call_args.args
    assert args[0] == "run_content_analysis"
    # 文件真落盘
    async with db_session_factory() as s:
        row = (await s.execute(select(ContentAnalysis))).scalars().first()
        from pathlib import Path
        assert Path(row.samples[0].video_path).exists()


async def test_get_and_list_analysis(client, db_session_factory):
    async with db_session_factory() as s:
        a = ContentAnalysis(title="t"); s.add(a); await s.commit(); aid = a.id
    r = await client.get(f"/api/analyses/{aid}", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["id"] == aid
    r2 = await client.get("/api/analyses", headers=HEADERS)
    assert r2.status_code == 200
    assert r2.json()["total"] >= 1


async def test_attach_brief_snapshots_into_project(client, db_session_factory):
    async with db_session_factory() as s:
        a = ContentAnalysis(title="t", status="completed",
                            brief_json='{"screenwriter_directives":"开场抛悬念"}')
        p = Project(title="p", theme_text="th", creator_name="test-user",
                    status=ProjectStatus.DRAFT.value)
        s.add_all([a, p]); await s.commit(); aid, pid = a.id, p.id
    r = await client.post(f"/api/projects/{pid}/attach-brief",
                          json={"analysis_id": aid}, headers=HEADERS)
    assert r.status_code == 200
    async with db_session_factory() as s:
        p = (await s.execute(select(Project).where(Project.id == pid))).scalar_one()
        assert p.content_analysis_id == aid
        assert json.loads(p.attached_brief_json)["screenwriter_directives"] == "开场抛悬念"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest tests/integration/test_content_analysis_api.py -v`
Expected: FAIL — 404（路由未注册）

- [ ] **Step 3: 写 router**

`backend/app/api/content_analysis.py`：
```python
"""内容分析 API：建分析+上传参考视频、列表、详情、SSE、挂载 brief 到 project。"""

import json
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from arq.connections import ArqRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.api.stream import get_redis  # 复用现有 redis 依赖
from app.models.project import ContentAnalysis, ReferenceSample, Project
from app.models.schemas import (
    ContentAnalysisResponse, ContentAnalysisList, AttachBriefRequest,
)
from app.models.schemas import ProjectResponse
from app.services.storage import sample_dir
from app.services.events import subscribe_to_events

router = APIRouter()


async def _get_arq_redis(redis) -> ArqRedis:
    return ArqRedis(redis.connection_pool)


def _require_user(x_user_name: str = None) -> str:
    return x_user_name or "anonymous"


@router.post("/analyses", response_model=ContentAnalysisResponse, status_code=201)
async def create_analysis(
    title: str = Form(...),
    region_hint: Optional[str] = Form(default=None),
    files: List[UploadFile] = File(...),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    if not files:
        raise HTTPException(status_code=400, detail="至少上传一个参考视频")
    analysis = ContentAnalysis(title=title, region_hint=region_hint)
    session.add(analysis)
    await session.flush()  # 拿 analysis.id

    for idx, upload in enumerate(files):
        smp = ReferenceSample(analysis_id=analysis.id, order_index=idx, video_path="")
        session.add(smp)
        await session.flush()  # 拿 smp.id
        dest_dir = sample_dir(analysis.id, smp.id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe_name = (upload.filename or f"sample_{idx}.mp4").replace("/", "_")
        dest = dest_dir / f"source_{safe_name}"
        dest.write_bytes(await upload.read())
        smp.video_path = str(dest)

    await session.commit()
    await session.refresh(analysis)

    arq = await _get_arq_redis(redis)
    await arq.enqueue_job("run_content_analysis", analysis.id, f"user:{_require_user()}")
    return analysis


@router.get("/analyses", response_model=ContentAnalysisList)
async def list_analyses(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(ContentAnalysis).order_by(ContentAnalysis.created_at.desc())
    )).scalars().all()
    return ContentAnalysisList(analyses=rows, total=len(rows))


@router.get("/analyses/{analysis_id}", response_model=ContentAnalysisResponse)
async def get_analysis(analysis_id: str, session: AsyncSession = Depends(get_session)):
    row = (await session.execute(
        select(ContentAnalysis).where(ContentAnalysis.id == analysis_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="分析不存在")
    return row


@router.get("/analyses/{analysis_id}/stream")
async def stream_analysis(analysis_id: str, redis=Depends(get_redis)):
    from sse_starlette.sse import EventSourceResponse

    async def event_generator():
        async for event in subscribe_to_events(redis, analysis_id):
            yield json.dumps(event)

    return EventSourceResponse(event_generator(), media_type="text/event-stream")


@router.post("/projects/{project_id}/attach-brief", response_model=ProjectResponse)
async def attach_brief(
    project_id: str,
    body: AttachBriefRequest,
    session: AsyncSession = Depends(get_session),
):
    project = (await session.execute(
        select(Project).where(Project.id == project_id)
    )).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    analysis = (await session.execute(
        select(ContentAnalysis).where(ContentAnalysis.id == body.analysis_id)
    )).scalar_one_or_none()
    if analysis is None:
        raise HTTPException(status_code=404, detail="分析不存在")
    if not analysis.brief_json:
        raise HTTPException(status_code=409, detail="该分析尚未产出 brief")
    project.content_analysis_id = analysis.id
    project.attached_brief_json = analysis.brief_json  # 快照
    await session.commit()
    await session.refresh(project)
    return project
```
> `ProjectResponse` 已存在于 `schemas.py`（`create_project` 用它）。`get_redis` 从 `app.api.stream` 导入（该模块已定义它）；若实际定义在别处，改为对应导入路径。`_require_user` 这里简化为固定 actor —— 与现有 `projects.py::_require_user`（读 `X-User-Name`）保持一致即可：如需真实用户，改为 `from app.api.projects import _require_user` 并加 `user: str = Depends(_require_user)` 到 `create_analysis`。

- [ ] **Step 4: 注册 router**

`backend/app/main.py`：import 行（约 124 行）加入 `content_analysis`：
```python
from app.api import (projects, pipeline, uploads, assets, stream, debug,
                     voice, image_candidates, content_analysis)
```
注册块（约 133 行后）加：
```python
app.include_router(content_analysis.router, prefix="/api")
```

- [ ] **Step 5: conftest 加 arq patch**

`backend/tests/integration/conftest.py`：在 `client` fixture 里、其它 `_get_arq_redis` patch 旁加：
```python
    from app.api import content_analysis as content_analysis_module
    monkeypatch.setattr(content_analysis_module, "_get_arq_redis", _fake_get_arq)
```

- [ ] **Step 6: 跑测试确认通过**

Run: `uv run --project backend pytest tests/integration/test_content_analysis_api.py -v`
Expected: PASS

- [ ] **Step 7: 全量回归**

Run: `uv run --project backend pytest -q`
Expected: 全绿（含既有测试未回归）

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/content_analysis.py backend/app/main.py backend/tests/integration/conftest.py backend/tests/integration/test_content_analysis_api.py
git commit -m "feat(api): 内容分析 router（建分析/上传/列表/详情/SSE/挂载 brief）"
```

---

## Task 9: 部署接线（worker 装 asr 组 + 模型缓存卷）

**Files:**
- Modify: `deploy/docker-compose.dev.yml`（worker 服务：装 `asr` 组、挂 HF 缓存卷、传 ASR env）

**Interfaces:** 无代码接口；使 worker 容器具备 faster-whisper 运行条件。

- [ ] **Step 1: 改 compose worker 服务**

在 `deploy/docker-compose.dev.yml` 的 worker 服务上：
1. 安装命令加 `--group asr`（worker 的 `uv sync`/启动命令处）。backend(API) 服务**不加**。
2. 加命名卷缓存 HF 权重，避免重下：
```yaml
    volumes:
      - ../backend:/app:z
      - whisper-cache:/root/.cache/huggingface
    environment:
      HF_HOME: /root/.cache/huggingface
      ASR_DEVICE: cpu
      ASR_COMPUTE_TYPE: int8
```
3. 文件底部 `volumes:` 段加：
```yaml
volumes:
  whisper-cache:
```
> 具体键名/缩进以该 compose 现有 worker 服务与 volumes 段为准，只做最小新增。首次运行 worker 会下载 ~3GB 权重到该卷。

- [ ] **Step 2: 校验 compose 合法**

Run: `podman compose -f deploy/docker-compose.dev.yml config >/dev/null && echo OK`
Expected: `OK`（YAML 合法、变量可解析）

- [ ] **Step 3: Commit**

```bash
git add deploy/docker-compose.dev.yml
git commit -m "chore(deploy): worker 装 asr 组 + HF 模型缓存卷 + ASR env"
```

---

## 后续计划（不在本 plan）

- **前端 UI（spec §10）**：内容分析列表/新建/上传/SSE 进度/brief 展示，新建 project 的「挂载 brief」选择器（`frontend-vite`）。
- **Playwright UI e2e（spec §9）**：上传真 fixture 视频，仅短路 brief LLM，断言真实 analysis→brief→挂 project→screenwriter 收到 brief。
- 各自新建 spec/plan。

---

## Self-Review（作者自查，已执行）

**Spec 覆盖：**
- §4 数据模型 → Task 1 ✓；§4.3 projects 挂钩列 + 迁移 → Task 1 ✓
- §4.4 存储布局 + §12 配置项 → Task 2 ✓
- §5 Step2 转写（VAD+词级 hook）→ Task 4 ✓；§5 Step3 归纳 → Task 5 ✓
- §7 screenwriter 注入 + worker 传参 → Task 6 ✓
- §5 完整 3 步管线 + §8 无人声/逐样本容错/音频即删 → Task 7 ✓
- API（建分析/上传/详情/SSE/挂载）→ Task 8 ✓；§9 测试只在 brief LLM 打桩 → Task 5/7/8 ✓
- §12 部署（asr 组/缓存卷/env）→ Task 9 ✓
- §10 前端 + §9 Playwright e2e → 明确列为后续计划 ✓（本 plan 交付后端可测垂直切片）

**占位符扫描：** 初稿在 Task 6 留了一个「先占位、后替换」的测试体，已修正为标准 TDD（Step 5 直接写真实失败测试 → Step 6 确认失败 → Step 7 实现 → Step 8 通过）。当前全plan无 TBD/TODO/占位断言，每个 code step 均含可运行代码，每个 run step 均含确切命令与预期输出。

**类型一致性：** `run_content_analysis` 任务名字符串在 Task 7 定义、Task 8 入队处一致；`TranscriptResult` 字段（has_speech/full_transcript/hook_text/language）在 Task 4 定义、Task 7 stub 与消费一致；`creation_brief` 参数名在 Task 6 screenwriter 与 worker 传参一致；`_get_arq_redis` 按模块 patch 在 Task 8 与 conftest 一致。
