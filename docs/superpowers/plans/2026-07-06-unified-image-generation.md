# 统一图片生成服务 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把尾帧生成 / 首帧生成 / CC 人脸校准三个平行 Gemini 调用点收编为统一 `image_generation` 服务，并引入"生成→候选→采纳"画廊流（`ImageCandidate` 表 + 统一生成弹窗）。

**Architecture:** 后端一个服务模块（共享 Vertex client/超时/图片提取/裁剪/观测 + 四种模式 tail_frame / first_frame / custom / cc_edit）、一张 `ImageCandidate` 表（候选文件在 `{shot_dir}/candidates/`）、一个 ARQ 任务 `run_image_candidate`、一组 REST 端点（创建/采纳/删除候选）。旧的 `run_tail_frame_pipeline` / `run_first_frame_pipeline` 删除，旧端点变薄 wrapper。前端复用关键帧下拉，新增统一生成弹窗与 CC 候选条。

**Tech Stack:** FastAPI + SQLAlchemy(async, SQLite) + ARQ + Redis pub/sub SSE；google-genai (Vertex AI)；React + TypeScript (Vite, shadcn/ui)；pytest + Playwright。

**Spec:** `docs/superpowers/specs/2026-07-06-unified-image-generation-design.md`
**设计稿:** `design/exports/04-generate-image-dialog.png`、`05-keyframe-dropdown-generate.png`、`06-cc-candidates-strip.png`

## Global Constraints

- **google.genai 必须 `vertexai=True` + service account**，禁止 API key（CLAUDE.md）。
- **所有模型调用在测试中必须 mock**（Gemini 文本/图像都算）；e2e 只允许 stub AI 触发端点（返回真实 202 形状），不得 mock 被测数据流。
- **不用 fakeredis**：集成测试用真实 Redis `redis://localhost:6381/15`（现有 conftest fixture）。
- **路径即真相**：槽位字段 `custom_first_frame_path` / `target_last_frame_path` / `last_frame_path`；采纳 = 复制候选文件入槽，候选原件不动。
- **候选永不自动清理**；随 shot/project 删除级联。
- **禁止硬编码绝对路径**；文件名一律 `ts_uuid_name()`。
- **后端测试**：`cd backend && uv run pytest ...`（不经 podman）。
- **改完后端代码要 `podman restart video-maker-backend-dev video-maker-worker-dev`**（本地验证时）。
- 每次生成产 **1 张**候选；候选生成**不触碰 project 状态机**。
- 提交信息用 conventional commits（feat/fix/test/refactor/docs）。

---

### Task 1: ImageCandidate 模型 + candidates 目录 + 序列化

**Files:**
- Modify: `backend/app/models/project.py`（新增 ImageCandidate、Shot 加 relationship）
- Modify: `backend/app/services/storage.py`（新增 `shot_candidates_dir`）
- Modify: `backend/app/api/projects.py:24-68`（`_candidate_to_dict` + `_shot_to_dict` 加字段）
- Test: `backend/tests/unit/test_image_candidate_model.py`

**Interfaces:**
- Produces: `ImageCandidate`（字段见下）；`Shot.image_candidates`（`lazy="selectin"`，任何加载 Shot 的地方自动带出，无需改动各 selectinload 调用点）；`shot_candidates_dir(project_id: str, shot_id: int) -> Path`；`_candidate_to_dict(c) -> dict`；`_shot_to_dict` 返回值多一个 `"image_candidates": [dict]`。
- 新表由 `Base.metadata.create_all` 自动建表，**无需**在 `db.py._run_migrations` 加 ALTER。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/unit/test_image_candidate_model.py
"""ImageCandidate 模型 + 序列化 + candidates 目录 helper."""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.project import Base, Project, Shot


@pytest.fixture
async def sf():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    await engine.dispose()


async def _seed(sf):
    async with sf() as s:
        p = Project(title="t", theme_text="t", creator_name="u", status="shot_review")
        s.add(p)
        await s.flush()
        shot = Shot(
            project_id=p.id, shot_id=1, text="hi", shot_type="Medium Shot",
            visual_description="v", shot_duration=6, status="completed",
        )
        s.add(shot)
        await s.commit()
        return p.id, shot.id


@pytest.mark.asyncio
async def test_candidate_roundtrip_and_defaults(sf):
    from app.models.project import ImageCandidate
    pid, shot_pk = await _seed(sf)
    async with sf() as s:
        c = ImageCandidate(project_id=pid, shot_pk=shot_pk, shot_id=1, slot="tail_frame")
        s.add(c)
        await s.commit()
        row = (await s.execute(select(ImageCandidate))).scalar_one()
        assert row.status == "generating"
        assert row.prompt_source == "auto"
        assert row.adopted_at is None
        assert len(row.id) == 36


@pytest.mark.asyncio
async def test_shot_relationship_selectin_and_cascade(sf):
    from app.models.project import ImageCandidate
    pid, shot_pk = await _seed(sf)
    async with sf() as s:
        s.add(ImageCandidate(project_id=pid, shot_pk=shot_pk, shot_id=1, slot="cc"))
        await s.commit()
    async with sf() as s:
        shot = (await s.execute(select(Shot).where(Shot.id == shot_pk))).scalar_one()
        assert len(shot.image_candidates) == 1  # lazy="selectin" 自动加载
        await s.delete(shot)
        await s.commit()
        assert (await s.execute(select(ImageCandidate))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_shot_to_dict_includes_candidates(sf):
    from app.models.project import ImageCandidate
    from app.api.projects import _shot_to_dict
    pid, shot_pk = await _seed(sf)
    async with sf() as s:
        s.add(ImageCandidate(
            project_id=pid, shot_pk=shot_pk, shot_id=1, slot="tail_frame",
            status="done", file_path="/nonstorage/x.png", custom_prompt="p",
            prompt_source="custom",
        ))
        await s.commit()
        shot = (await s.execute(select(Shot).where(Shot.id == shot_pk))).scalar_one()
        d = _shot_to_dict(shot)
        assert len(d["image_candidates"]) == 1
        c = d["image_candidates"][0]
        assert c["slot"] == "tail_frame"
        assert c["status"] == "done"
        assert c["prompt_source"] == "custom"
        assert c["custom_prompt"] == "p"
        assert "id" in c and "created_at" in c and "adopted_at" in c


def test_shot_candidates_dir():
    from app.services.storage import shot_candidates_dir, shot_dir
    assert shot_candidates_dir("pid", 3) == shot_dir("pid", 3) / "candidates"
```

- [ ] **Step 2: 跑测试确认失败**

Run（`backend/` 下）: `uv run pytest tests/unit/test_image_candidate_model.py -v`
Expected: FAIL `ImportError: cannot import name 'ImageCandidate'`

- [ ] **Step 3: 实现**

`backend/app/models/project.py` — 在 `Shot` 类后新增（`Shot.__table_args__` 之后）：

```python
class ImageCandidate(Base):
    __tablename__ = "image_candidates"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    shot_pk = Column(
        Integer,
        ForeignKey("shots.id", ondelete="CASCADE"),
        nullable=False,
    )
    shot_id = Column(Integer, nullable=False)  # Shot.shot_id 序号（冗余便于查询/事件）
    slot = Column(String(20), nullable=False)  # 'first_frame' | 'tail_frame' | 'cc'
    status = Column(String(20), nullable=False, default="generating")  # generating|done|failed
    file_path = Column(Text, nullable=True)
    prompt_source = Column(String(10), nullable=False, default="auto")  # auto|custom
    custom_prompt = Column(Text, nullable=True)
    ref_paths = Column(Text, nullable=True)  # JSON: {"character": [...], "object": [...]}
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    adopted_at = Column(DateTime, nullable=True)

    shot = relationship("Shot", back_populates="image_candidates")

    __table_args__ = (
        Index("ix_image_candidates_shot", "project_id", "shot_id"),
    )
```

`Shot` 类的 relationships 区（`project = relationship(...)` 旁）加：

```python
    image_candidates = relationship(
        "ImageCandidate",
        back_populates="shot",
        cascade="all, delete-orphan",
        order_by="ImageCandidate.created_at",
        lazy="selectin",
    )
```

`backend/app/services/storage.py` — `shot_custom_frames_dir` 后加：

```python
def shot_candidates_dir(project_id: str, shot_id: int) -> Path:
    """Image-candidate gallery dir for a shot (generated candidates + temp ref uploads)."""
    return shot_dir(project_id, shot_id) / "candidates"
```

`backend/app/api/projects.py` — `_shot_to_dict` 前加，并在其返回 dict 中 `"tf_confirmed"` 行后插入一行：

```python
def _candidate_to_dict(c) -> dict:
    """Serialize an ImageCandidate for the API."""
    return {
        "id": c.id,
        "shot_id": c.shot_id,
        "slot": c.slot,
        "status": c.status,
        "file_path": to_media_url(c.file_path),
        "prompt_source": c.prompt_source,
        "custom_prompt": c.custom_prompt,
        "error": c.error,
        "created_at": c.created_at,
        "adopted_at": c.adopted_at,
    }
```

```python
        "image_candidates": [_candidate_to_dict(c) for c in s.image_candidates],
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_image_candidate_model.py -v`
Expected: 4 PASS

- [ ] **Step 5: 回归**（序列化相关旧测试）

Run: `uv run pytest tests/unit/test_shot_serialization.py tests/integration/test_projects.py -v`
Expected: PASS（`lazy="selectin"` 不需要改任何 selectinload 调用点）

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/project.py backend/app/services/storage.py backend/app/api/projects.py backend/tests/unit/test_image_candidate_model.py
git commit -m "feat(image-candidates): ImageCandidate 模型 + candidates 目录 + 序列化"
```

---

### Task 2: 统一服务底座 `image_generation.py` + custom 模式

**Files:**
- Create: `backend/app/services/image_generation.py`
- Test: `backend/tests/unit/test_image_generation_core.py`

**Interfaces:**
- Produces:
  - `get_client(project: str | None = None, location: str | None = None) -> genai.Client` — 按 (project, location) 记忆化；缺省 `settings.tf_project`/`settings.tf_location`；必须 `vertexai=True`。
  - `_call_with_timeout(coro_factory, *, label: str, timeout: int = 120)`（从 tail_frame_generator 原样移植）
  - `_extract_text(response) -> str`、`_mime_for(path) -> str`（原样移植）
  - `parts_from_paths(paths: list[str] | None) -> list` — 跳过不存在的文件。
  - `run_image_step(*, image_parts: list, prompt: str, output_path: str, span_name: str, model: str | None = None, aspect_ratio: str | None = None, pin_aspect: bool = False, temperature: float | None = None, client=None) -> str` — 统一的"图像步"：generate_content(IMAGE) + 空响应 block_reason 处理 + 写文件 +（aspect_ratio 非空时）center_crop。
  - `generate_custom(prompt: str, output_path: str, character_ref_paths=None, object_ref_paths=None, context_frame_path=None, aspect_ratio="9:16") -> str` — 单步直出（不走 CoT）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/unit/test_image_generation_core.py
"""统一图片服务底座：run_image_step / parts_from_paths / generate_custom（mock genai client）."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_client_returning(parts):
    resp = MagicMock()
    resp.parts = parts
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(return_value=resp)
    return client


def _image_part(data=b"\x89PNGfake"):
    part = MagicMock()
    part.inline_data.data = data
    part.text = None
    return part


def test_parts_from_paths_skips_missing(tmp_path):
    from app.services.image_generation import parts_from_paths
    ok = tmp_path / "a.png"; ok.write_bytes(b"\x89PNG")
    parts = parts_from_paths([str(ok), str(tmp_path / "missing.png")])
    assert len(parts) == 1


def test_parts_from_paths_none():
    from app.services.image_generation import parts_from_paths
    assert parts_from_paths(None) == []


@pytest.mark.asyncio
async def test_run_image_step_writes_file(tmp_path):
    from app.services.image_generation import run_image_step
    out = tmp_path / "out.png"
    client = _mock_client_returning([_image_part(b"IMGDATA")])
    with patch("app.services.image_generation.center_crop_to_aspect") as crop:
        result = await run_image_step(
            image_parts=[], prompt="p", output_path=str(out),
            span_name="test-span", aspect_ratio="9:16", client=client,
        )
    assert result == str(out)
    assert out.read_bytes() == b"IMGDATA"
    crop.assert_called_once_with(str(out), "9:16")


@pytest.mark.asyncio
async def test_run_image_step_no_crop_when_no_aspect(tmp_path):
    from app.services.image_generation import run_image_step
    out = tmp_path / "out.png"
    client = _mock_client_returning([_image_part()])
    with patch("app.services.image_generation.center_crop_to_aspect") as crop:
        await run_image_step(
            image_parts=[], prompt="p", output_path=str(out),
            span_name="test-span", client=client,
        )
    crop.assert_not_called()


@pytest.mark.asyncio
async def test_run_image_step_empty_parts_raises(tmp_path):
    from app.services.image_generation import run_image_step
    client = _mock_client_returning([])
    with pytest.raises(RuntimeError, match="blocked or filtered"):
        await run_image_step(
            image_parts=[], prompt="p", output_path=str(tmp_path / "x.png"),
            span_name="test-span", client=client,
        )


@pytest.mark.asyncio
async def test_generate_custom_part_order_and_prompt(tmp_path):
    """custom 模式：图片顺序 = context → object → character，提示词就是用户提示词。"""
    from app.services import image_generation as ig
    ctx = tmp_path / "ctx.png"; ctx.write_bytes(b"C")
    obj = tmp_path / "obj.png"; obj.write_bytes(b"O")
    char = tmp_path / "char.png"; char.write_bytes(b"H")
    out = tmp_path / "out.png"
    client = _mock_client_returning([_image_part()])
    with patch.object(ig, "center_crop_to_aspect"):
        await ig.generate_custom(
            prompt="my custom prompt",
            output_path=str(out),
            character_ref_paths=[str(char)],
            object_ref_paths=[str(obj)],
            context_frame_path=str(ctx),
            aspect_ratio="9:16",
        )
    call = client.aio.models.generate_content.await_args
    parts = call.kwargs["contents"][0].parts
    # 最后一个 part 是文本提示词，且原样使用用户输入
    assert parts[-1].text == "my custom prompt"
    # 图片顺序：context(C) → object(O) → character(H)
    datas = [p.inline_data.data for p in parts[:-1]]
    assert datas == [b"C", b"O", b"H"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_image_generation_core.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'app.services.image_generation'`

- [ ] **Step 3: 实现 `backend/app/services/image_generation.py`**

```python
"""统一图片生成服务 — 尾帧 / 首帧 / 自定义 / CC 编辑共用一个底座。

共享：Vertex genai client（按 project/location 记忆化）、超时包装、
图像步（generate_content IMAGE + 空响应处理 + 写文件 + 裁剪）、观测。
模式函数在后续任务中从 tail_frame_generator / first_frame_generator /
face_calibration_client 迁移进来。
"""

import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import Awaitable, Callable, List, Optional

from google import genai
from google.genai import types

from app.agents.frame_porter import center_crop_to_aspect
from app.config import settings
from app import observability

logger = logging.getLogger(__name__)

_clients: dict[tuple[str, str], genai.Client] = {}


def get_client(project: Optional[str] = None, location: Optional[str] = None) -> genai.Client:
    """Memoized Vertex client per (project, location). Never uses API keys."""
    key = (project or settings.tf_project, location or settings.tf_location)
    if key not in _clients:
        _clients[key] = genai.Client(vertexai=True, project=key[0], location=key[1])
    return _clients[key]


def _mime_for(path: str) -> str:
    mt, _ = mimetypes.guess_type(path)
    return mt or "image/png"


def _extract_text(response) -> str:
    out = ""
    for part in response.parts:
        if part.text:
            out += part.text
    return out


async def _call_with_timeout(
    coro_factory: Callable[[], Awaitable], *, label: str, timeout: int = 120
):
    """Await a model call with a hard timeout, turning a timeout into a clear error."""
    try:
        return await asyncio.wait_for(coro_factory(), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"{label} timed out after {timeout}s (network issue)")


def parts_from_paths(paths: Optional[List[str]]) -> list:
    """Image Parts from file paths; silently skips missing files."""
    parts: list = []
    for p in paths or []:
        fp = Path(p)
        if fp.exists():
            parts.append(
                types.Part.from_bytes(data=fp.read_bytes(), mime_type=_mime_for(p))
            )
    return parts


async def run_image_step(
    *,
    image_parts: list,
    prompt: str,
    output_path: str,
    span_name: str,
    model: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    pin_aspect: bool = False,
    temperature: Optional[float] = None,
    client: Optional[genai.Client] = None,
) -> str:
    """One IMAGE generation call: parts+prompt → image file (+ optional center-crop).

    aspect_ratio=None 表示不裁剪（CC 编辑保持原图尺寸）。pin_aspect=True 时
    额外用 ImageConfig 钉住输出方向（首帧生成的既有行为）。
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    client = client or get_client()
    model = model or settings.tf_model

    config_kwargs: dict = {"response_modalities": ["IMAGE"]}
    if temperature is not None:
        config_kwargs["temperature"] = temperature
    if pin_aspect and aspect_ratio:
        config_kwargs["image_config"] = types.ImageConfig(aspect_ratio=aspect_ratio)

    all_parts = image_parts + [types.Part(text=prompt)]
    model_params = dict(config_kwargs)
    model_params.pop("image_config", None)

    with observability.generation(
        name=span_name,
        model=model,
        input={"prompt": prompt[:500], "num_image_parts": len(image_parts)},
        model_parameters=model_params,
    ) as gen:
        response = await _call_with_timeout(
            lambda: client.aio.models.generate_content(
                model=model,
                contents=[types.Content(role="user", parts=all_parts)],
                config=types.GenerateContentConfig(**config_kwargs),
            ),
            label=f"{span_name} API call",
        )

        saved = False
        parts = response.parts or []
        if not parts:
            block_reason = getattr(response, "prompt_feedback", None)
            candidates = getattr(response, "candidates", None)
            finish_reason = None
            if candidates:
                finish_reason = getattr(candidates[0], "finish_reason", None)
            logger.error(
                "%s returned no parts. block_reason=%s finish_reason=%s candidates=%s",
                span_name, block_reason, finish_reason, candidates,
            )
            raise RuntimeError(
                f"Gemini returned empty response (blocked or filtered). "
                f"block_reason={block_reason}, finish_reason={finish_reason}"
            )

        for part in parts:
            if part.inline_data is not None:
                Path(output_path).write_bytes(part.inline_data.data)
                saved = True
                logger.info("%s: saved %s", span_name, output_path)
                break
            if part.text is not None:
                logger.info("%s text response: %s", span_name, part.text[:200])

        if not saved:
            raise RuntimeError(
                "Gemini did not return an image. "
                f"Response parts: {[type(p).__name__ for p in parts]}"
            )

        observability.update_span(gen, output={"output_path": output_path})

    if aspect_ratio:
        center_crop_to_aspect(output_path, aspect_ratio)
    return output_path


async def generate_custom(
    prompt: str,
    output_path: str,
    character_ref_paths: Optional[List[str]] = None,
    object_ref_paths: Optional[List[str]] = None,
    context_frame_path: Optional[str] = None,
    aspect_ratio: str = "9:16",
) -> str:
    """自定义提示词单步直出：用户提示词即最终提示词，不走 CoT。

    图片顺序沿用两步链的约定：context → object → character（身份最后、最强条件）。
    """
    image_parts = parts_from_paths([context_frame_path] if context_frame_path else [])
    image_parts += parts_from_paths(object_ref_paths)
    image_parts += parts_from_paths(character_ref_paths)
    return await run_image_step(
        image_parts=image_parts,
        prompt=prompt,
        output_path=output_path,
        span_name="services-custom-image-generate",
        aspect_ratio=aspect_ratio,
        pin_aspect=True,
        temperature=1.0,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_image_generation_core.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/image_generation.py backend/tests/unit/test_image_generation_core.py
git commit -m "feat(image-gen): 统一图片服务底座 + custom 单步直出模式"
```

---

### Task 3: 迁移 tail_frame 模式，删除 `tail_frame_generator.py`

**Files:**
- Modify: `backend/app/services/image_generation.py`（加 `generate_tail_frame`）
- Delete: `backend/app/services/tail_frame_generator.py`
- Modify: `backend/app/services/first_frame_generator.py:21-26`（改 import 来源）
- Modify: `backend/worker/tasks.py:642`（`from app.services.tail_frame_generator import generate_tail_frame` → `from app.services.image_generation import generate_tail_frame`）
- Modify: `backend/tests/unit/test_tail_frame.py`（patch 目标同步改名）
- Test: 复用 `backend/tests/unit/test_tail_frame.py`

**Interfaces:**
- Produces: `image_generation.generate_tail_frame(character_ref_paths, first_frame_path, motion_prompt, output_path, object_ref_paths=None, aspect_ratio="9:16", on_cot_complete=None) -> str` — 签名与旧版**完全一致**。
- Consumes: Task 2 的 `get_client/parts_from_paths/run_image_step/_call_with_timeout/_extract_text`。

- [ ] **Step 1: 迁移实现**

把 `tail_frame_generator.py` 的 `generate_tail_frame`、`_COT_MIN_LEN`、`_COT_CONSERVATIVE_MARKERS`、`_is_cot_too_weak` 迁入 `image_generation.py`，做以下等价化简（其余逐行保留，包括 CoT 弱输出高温重掷与硬兜底、`on_cot_complete` 回调、tf 提示词、图片顺序注释）：

1. `char_parts`/`obj_parts`/`first_frame_parts` 的手工构造 → `parts_from_paths(character_ref_paths)` / `parts_from_paths(object_ref_paths)` / `parts_from_paths([first_frame_path] if first_frame_path else [])`。
2. Step 2 整段（`observability.generation("services-tail-frame-generate-image")` 到 `center_crop_to_aspect`）→

```python
    img_image_parts = first_frame_parts + obj_parts + char_parts
    img_prompt = settings.tf_prompt.format(motion_prompt=motion_prompt, end_pose=end_pose)
    await run_image_step(
        image_parts=img_image_parts,
        prompt=img_prompt,
        output_path=output_path,
        span_name="services-tail-frame-generate-image",
        aspect_ratio=aspect_ratio,
        pin_aspect=True,        # 行为保持：现行代码一直钉 ImageConfig 防横版返回+错裁（Task 3 评审勘误）
        temperature=1.0,
    )
    return output_path
```

3. CoT 段的 `client = _get_client()` → `client = get_client()`；两处 `client.aio.models.generate_content`（首次 + 重掷）保持原样（含 temperature 0.6/0.9、`observability.generation("services-tail-frame-cot-analysis")` span）。

删除 `backend/app/services/tail_frame_generator.py`。

`backend/app/services/first_frame_generator.py` 头部 import 改为：

```python
from app.services.image_generation import (
    _call_with_timeout,
    _extract_text,
    _mime_for,
    get_client as _get_client,
)
```

`backend/worker/tasks.py:642` 的函数内 import 改为 `from app.services.image_generation import generate_tail_frame`。

- [ ] **Step 2: 更新旧测试的 patch 目标**

`backend/tests/unit/test_tail_frame.py` 中所有 `patch("app.services.tail_frame_generator.X")` / `import app.services.tail_frame_generator` 改为 `app.services.image_generation`（用 `grep -n tail_frame_generator backend/tests/unit/test_tail_frame.py` 逐个确认）。

- [ ] **Step 3: 全量回归**

Run: `uv run pytest tests/unit/test_tail_frame.py tests/unit/test_image_generation_core.py tests/integration/test_tail_frame_pipeline.py tests/integration/test_first_frame_pipeline.py -v`
Expected: PASS；且 `grep -rn tail_frame_generator backend/ --include=*.py` 无结果

- [ ] **Step 4: Commit**

```bash
git add -A backend
git commit -m "refactor(image-gen): tail_frame 模式并入统一服务，删除 tail_frame_generator"
```

---

### Task 4: 迁移 first_frame 模式，删除 `first_frame_generator.py`

**Files:**
- Modify: `backend/app/services/image_generation.py`（加 `generate_first_frame`）
- Delete: `backend/app/services/first_frame_generator.py`
- Modify: `backend/worker/tasks.py:788`（import 改 `app.services.image_generation`）
- Modify: `backend/app/config.py:187`（`ff_cot_prompt` 末尾补反推措辞）
- Test: `backend/tests/integration/test_first_frame_pipeline.py`（patch 目标改名）

**Interfaces:**
- Produces: `image_generation.generate_first_frame(character_ref_paths, context_frame_path, visual_description, shot_type, output_path, motion_prompt=None, object_ref_paths=None, aspect_ratio="9:16") -> str` — 签名与旧版一致。context_frame_path 语义扩展：可能是"当前首帧"（顺推）也可能是"本镜尾帧"（反推）。

- [ ] **Step 1: 迁移实现**

把 `first_frame_generator.py` 的 `generate_first_frame` 迁入 `image_generation.py`，等价化简：

1. 三组 parts 构造 → `parts_from_paths(...)`（同 Task 3 方式）。
2. Step 2 图像段 →

```python
    img_image_parts = context_parts + obj_parts + char_parts
    img_prompt = settings.ff_prompt.format(
        visual_description=visual_description,
        opening_composition=opening_composition,
    )
    await run_image_step(
        image_parts=img_image_parts,
        prompt=img_prompt,
        output_path=output_path,
        span_name="services-first-frame-generate-image",
        aspect_ratio=aspect_ratio,
        pin_aspect=True,        # 保持旧行为：首帧钉 ImageConfig
        temperature=1.0,
    )
    return output_path
```

3. CoT 段保持原样（`ff_cot_prompt`、span `services-first-frame-cot-analysis`、空输出兜底）。

删除 `first_frame_generator.py`；`worker/tasks.py:788` 的 import 改为 `from app.services.image_generation import generate_first_frame`。

- [ ] **Step 2: ff_cot_prompt 反推措辞**

`backend/app/config.py` 的 `ff_cot_prompt`（:187 起的字符串）**末尾追加**下面这句话（按该字符串既有的多行拼接风格接在最后一段，原有内容一字不动）：

> Note: the provided context frame (if any) may be either the CURRENT opening frame or the shot's ENDING frame. If it is the ending frame, reason BACKWARDS: derive an opening composition that would naturally evolve into that ending state.

- [ ] **Step 3: 更新集成测试 patch 目标并回归**

`backend/tests/integration/test_first_frame_pipeline.py` 中 `first_frame_generator` 的 patch 目标改为 `app.services.image_generation`（`grep -n first_frame_generator backend/tests/`）。

Run: `uv run pytest tests/integration/test_first_frame_pipeline.py tests/unit/test_image_generation_core.py -v`
Expected: PASS；`grep -rn first_frame_generator backend/ --include=*.py` 无结果

- [ ] **Step 4: Commit**

```bash
git add -A backend
git commit -m "refactor(image-gen): first_frame 模式并入统一服务，CoT 支持尾帧反推措辞"
```

---

### Task 5: 迁移 cc_edit 模式，删除 `face_calibration_client.py`

**Files:**
- Modify: `backend/app/services/image_generation.py`（加 `calibrate_face`）
- Delete: `backend/app/services/face_calibration_client.py`
- Modify: `backend/worker/tasks.py:1131`（import 改名）
- Test: `backend/tests/unit/test_calibrate_face.py`

**Interfaces:**
- Produces: `image_generation.calibrate_face(reference_image_paths: List[str], source_frame_path: str, output_frame_path: str) -> str` — 签名与旧版一致；client 用 `get_client(settings.cc_project, settings.cc_location)`；模型 `settings.cc_model`；**不裁剪**（保持原图尺寸）；图片顺序 = 身份参考在前、BASE 帧最后（旧行为，勿反）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/unit/test_calibrate_face.py
"""cc_edit 模式：part 顺序（refs 前、BASE 帧最后）+ 不裁剪 + cc 模型."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_calibrate_face_order_model_and_no_crop(tmp_path):
    from app.services import image_generation as ig
    from app.config import settings

    ref = tmp_path / "ref.png"; ref.write_bytes(b"R")
    src = tmp_path / "last_frame.png"; src.write_bytes(b"S")
    out = tmp_path / "cc_out.png"

    part = MagicMock(); part.inline_data.data = b"CAL"; part.text = None
    resp = MagicMock(); resp.parts = [part]
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(return_value=resp)

    with patch.object(ig, "get_client", return_value=client) as gc, \
         patch.object(ig, "center_crop_to_aspect") as crop:
        await ig.calibrate_face([str(ref)], str(src), str(out))

    gc.assert_called_once_with(settings.cc_project, settings.cc_location)
    crop.assert_not_called()
    assert out.read_bytes() == b"CAL"

    call = client.aio.models.generate_content.await_args
    assert call.kwargs["model"] == settings.cc_model
    parts = call.kwargs["contents"][0].parts
    assert parts[-1].text == settings.cc_prompt
    assert [p.inline_data.data for p in parts[:-1]] == [b"R", b"S"]  # ref 前、BASE 最后
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_calibrate_face.py -v`
Expected: FAIL `ImportError`（image_generation 无 calibrate_face）

- [ ] **Step 3: 实现**

`image_generation.py` 加（保留旧模块 docstring 中"帧必须放最后"的注释）：

```python
async def calibrate_face(
    reference_image_paths: List[str],
    source_frame_path: str,
    output_frame_path: str,
) -> str:
    """CC 人脸校准（edit 模式）：只换脸部身份，姿态/背景保持 BASE 帧。

    身份参考在前、BASE 帧最后 — 模型把 "edit-this-image" 锚定在最后一张图上，
    帧在最后其姿态才被保留（帧在前会导致复制参考图姿态）。不做裁剪。
    """
    image_parts = parts_from_paths(reference_image_paths)
    image_parts += parts_from_paths([source_frame_path])
    logger.info(
        "CC: calling %s refs=%d frame=%s",
        settings.cc_model, len(reference_image_paths), source_frame_path,
    )
    return await run_image_step(
        image_parts=image_parts,
        prompt=settings.cc_prompt,
        output_path=output_frame_path,
        span_name="services-face-calibration-generate-image",
        model=settings.cc_model,
        aspect_ratio=None,
        client=get_client(settings.cc_project, settings.cc_location),
    )
```

删除 `face_calibration_client.py`；`worker/tasks.py:1131` 改 `from app.services.image_generation import calibrate_face`。

- [ ] **Step 4: 回归**

Run: `uv run pytest tests/unit/ -v` 和 `uv run pytest tests/integration/ -x -q`
Expected: PASS；`grep -rn face_calibration_client backend/ --include=*.py` 无结果

- [ ] **Step 5: Commit**

```bash
git add -A backend
git commit -m "refactor(image-gen): cc_edit 模式并入统一服务，删除 face_calibration_client"
```

---

### Task 6: API — 创建候选 + 删除候选

**Files:**
- Create: `backend/app/api/image_candidates.py`
- Modify: `backend/app/main.py:126-132`（注册 router）
- Modify: `backend/tests/integration/conftest.py:70-73`（新 namespace 的 `_get_arq_redis` patch）
- Test: `backend/tests/integration/test_image_candidates_api.py`

**Interfaces:**
- Produces:
  - `POST /api/projects/{pid}/shots/{sid}/image-candidates` (202, multipart form)：`slot`(必填, first_frame|tail_frame)、`custom_prompt?`、`ref_image_ids?`(JSON 数组字符串, ReferenceImage.id)、`include_shot_refs?`(bool, 默认 true)、`files[]?`(临时参考图)。→ 建行(status=generating) + `enqueue_job("run_image_candidate", pid, sid, candidate_id, actor)` → 返回 `{"status": "queued", "candidate": {...}}`。
  - `DELETE /api/projects/{pid}/shots/{sid}/image-candidates/{cid}`：generating→409；否则删行 + unlink 文件 → `{"deleted": cid}`。
  - `ref_paths` JSON 约定：`{"character": [...], "object": [...]}`；键**存在即显式**（worker 只在键缺失时才用默认 character 参考图）；临时上传文件存 `candidates/ref_<ts_uuid>` 并归入 object。
- Consumes: Task 1 的 `ImageCandidate`、`shot_candidates_dir`、`_candidate_to_dict`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/integration/test_image_candidates_api.py
"""候选创建/删除端点（ARQ mock、真实 in-memory DB）."""
import io
import json
import pytest
from sqlalchemy import select

from tests.integration.conftest import HEADERS, USER, _make_project, _add_shot, _add_character_image
from app.models.project import ImageCandidate, ReferenceImage


async def _create(client, pid, shot_id=1, data=None, files=None):
    return await client.post(
        f"/api/projects/{pid}/shots/{shot_id}/image-candidates",
        data=data or {"slot": "tail_frame"},
        files=files,
        headers=HEADERS,
    )


async def test_create_auto_candidate_enqueues_worker(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)

    r = await _create(client, pid)
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    cand = body["candidate"]
    assert cand["slot"] == "tail_frame"
    assert cand["status"] == "generating"
    assert cand["prompt_source"] == "auto"

    client.arq.enqueue_job.assert_called_once_with(
        "run_image_candidate", pid, 1, cand["id"], f"user:{USER}"
    )


async def test_create_custom_candidate_with_temp_upload(client, db_session_factory, tmp_path):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)

    r = await _create(
        client, pid,
        data={"slot": "first_frame", "custom_prompt": "少女转身", "include_shot_refs": "false"},
        files=[("files", ("extra.png", io.BytesIO(b"\x89PNGx"), "image/png"))],
    )
    assert r.status_code == 202
    cand = r.json()["candidate"]
    assert cand["prompt_source"] == "custom"
    assert cand["custom_prompt"] == "少女转身"

    async with db_session_factory() as s:
        row = (await s.execute(select(ImageCandidate))).scalar_one()
        refs = json.loads(row.ref_paths)
        assert len(refs["object"]) == 1
        assert "candidates" in refs["object"][0]  # 临时上传进 candidates 目录
        from pathlib import Path
        assert Path(refs["object"][0]).read_bytes() == b"\x89PNGx"
        # 未产生 ReferenceImage 行
        assert (await s.execute(select(ReferenceImage))).scalar_one_or_none() is None


async def test_create_resolves_ref_image_ids_by_kind(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    img_id = await _add_character_image(db_session_factory, pid)

    r = await _create(client, pid, data={
        "slot": "tail_frame",
        "ref_image_ids": json.dumps([img_id]),
    })
    assert r.status_code == 202
    async with db_session_factory() as s:
        row = (await s.execute(select(ImageCandidate))).scalar_one()
        refs = json.loads(row.ref_paths)
        assert refs["character"] == [f"/fake/{pid}/test.jpg"]


async def test_create_rejects_bad_slot(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    r = await _create(client, pid, data={"slot": "cc"})
    assert r.status_code == 400  # cc 候选只能由校准端点产生


async def test_delete_candidate(client, db_session_factory, tmp_path):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    r = await _create(client, pid)
    cid = r.json()["candidate"]["id"]

    # generating → 409
    r2 = await client.delete(
        f"/api/projects/{pid}/shots/1/image-candidates/{cid}", headers=HEADERS
    )
    assert r2.status_code == 409

    # done + 有文件 → 删行 + unlink
    f = tmp_path / "c.png"; f.write_bytes(b"x")
    async with db_session_factory() as s:
        row = (await s.execute(select(ImageCandidate).where(ImageCandidate.id == cid))).scalar_one()
        row.status = "done"; row.file_path = str(f)
        await s.commit()
    r3 = await client.delete(
        f"/api/projects/{pid}/shots/1/image-candidates/{cid}", headers=HEADERS
    )
    assert r3.status_code == 200
    assert not f.exists()
    async with db_session_factory() as s:
        assert (await s.execute(select(ImageCandidate))).scalar_one_or_none() is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/integration/test_image_candidates_api.py -v`
Expected: FAIL（404，路由不存在）

- [ ] **Step 3: 实现 `backend/app/api/image_candidates.py`**

```python
"""图片候选端点 — 统一图片生成（生成→候选→采纳）。"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.main import get_redis
from app.models.project import ImageCandidate, Project, ReferenceImage, Shot
from app.services.storage import shot_candidates_dir, to_media_url, ts_uuid_name

logger = logging.getLogger(__name__)
router = APIRouter()

VALID_CREATE_SLOTS = {"first_frame", "tail_frame"}


def _require_user(x_user_name: Optional[str] = Header(default=None)) -> str:
    if not x_user_name:
        raise HTTPException(status_code=400, detail="X-User-Name header required")
    return x_user_name


async def _get_project_or_404(project_id: str, session: AsyncSession) -> Project:
    result = await session.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def _get_shot_or_404(project_id: str, shot_id: int, session: AsyncSession) -> Shot:
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")
    return shot


async def _get_candidate_or_404(
    project_id: str, shot_id: int, candidate_id: str, session: AsyncSession
) -> ImageCandidate:
    result = await session.execute(
        select(ImageCandidate).where(
            ImageCandidate.id == candidate_id,
            ImageCandidate.project_id == project_id,
            ImageCandidate.shot_id == shot_id,
        )
    )
    cand = result.scalar_one_or_none()
    if not cand:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return cand


async def _get_arq_redis(redis) -> ArqRedis:
    from arq import create_pool
    from arq.connections import RedisSettings
    from app.config import settings
    return await create_pool(RedisSettings.from_dsn(settings.redis_url))


@router.post(
    "/projects/{project_id}/shots/{shot_id}/image-candidates", status_code=202
)
async def create_image_candidate(
    project_id: str,
    shot_id: int,
    slot: str = Form(...),
    custom_prompt: Optional[str] = Form(default=None),
    ref_image_ids: Optional[str] = Form(default=None),
    include_shot_refs: bool = Form(default=True),
    files: List[UploadFile] = File(default=[]),
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
):
    """创建一个图片候选并入队生成（每次 1 张；不触碰 project 状态机）。"""
    from app.api.projects import _candidate_to_dict

    if slot not in VALID_CREATE_SLOTS:
        raise HTTPException(
            status_code=400,
            detail=f"slot must be one of {sorted(VALID_CREATE_SLOTS)} (cc candidates are created by calibrate endpoints)",
        )
    await _get_project_or_404(project_id, session)
    shot = await _get_shot_or_404(project_id, shot_id, session)

    custom_prompt = (custom_prompt or "").strip() or None

    # ── 解析参考图选择 → ref_paths JSON（键存在即显式；全缺省则存 None）──
    selected_char: list[str] = []
    selected_obj: list[str] = []
    explicit = False

    if ref_image_ids is not None:
        explicit = True
        try:
            ids = json.loads(ref_image_ids)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="ref_image_ids must be a JSON array")
        if ids:
            rows = (await session.execute(
                select(ReferenceImage).where(
                    ReferenceImage.project_id == project_id,
                    ReferenceImage.id.in_(ids),
                )
            )).scalars().all()
            for r in rows:
                (selected_char if r.kind == "character" else selected_obj).append(r.storage_path)

    if include_shot_refs and shot.custom_reference_paths:
        explicit = True
        selected_obj += json.loads(shot.custom_reference_paths)

    if files:
        explicit = True
        cand_dir = shot_candidates_dir(project_id, shot_id)
        cand_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            ext = Path(f.filename or "x.png").suffix or ".png"
            dest = cand_dir / f"ref_{ts_uuid_name(ext)}"
            dest.write_bytes(await f.read())
            selected_obj.append(str(dest))

    ref_paths = (
        json.dumps({"character": selected_char, "object": selected_obj})
        if explicit else None
    )

    cand = ImageCandidate(
        project_id=project_id,
        shot_pk=shot.id,
        shot_id=shot_id,
        slot=slot,
        status="generating",
        prompt_source="custom" if custom_prompt else "auto",
        custom_prompt=custom_prompt,
        ref_paths=ref_paths,
    )
    session.add(cand)
    await session.commit()
    await session.refresh(cand)

    arq = await _get_arq_redis(redis)
    await arq.enqueue_job("run_image_candidate", project_id, shot_id, cand.id, f"user:{user}")

    return {"status": "queued", "candidate": _candidate_to_dict(cand)}


@router.delete("/projects/{project_id}/shots/{shot_id}/image-candidates/{candidate_id}")
async def delete_image_candidate(
    project_id: str,
    shot_id: int,
    candidate_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """删除候选：删行 + unlink 文件。生成中禁删。已采纳可删（槽位持副本）。"""
    await _get_project_or_404(project_id, session)
    cand = await _get_candidate_or_404(project_id, shot_id, candidate_id, session)
    if cand.status == "generating":
        raise HTTPException(status_code=409, detail="Candidate is still generating")
    if cand.file_path:
        Path(cand.file_path).unlink(missing_ok=True)
    await session.delete(cand)
    await session.commit()
    return {"deleted": candidate_id}
```

（`include_shot_refs` 默认 true 且 shot 无参考物时 `explicit` 不置位——注意上面代码里 `include_shot_refs and shot.custom_reference_paths` 才置位，符合"全缺省 → ref_paths=None → worker 用默认 character"。）

`backend/app/main.py:132` 后加：

```python
from app.api import image_candidates  # noqa: E402  (与其他 router import 放一起)
app.include_router(image_candidates.router, prefix="/api")
```

（import 放到文件顶部现有 `from app.api import ...` 处，`include_router` 放 :132 `debug.router` 之后。）

`backend/tests/integration/conftest.py` 在 voice patch（:73）后加：

```python
    import app.api.image_candidates as image_candidates_module
    monkeypatch.setattr(image_candidates_module, "_get_arq_redis", _fake_get_arq)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_image_candidates_api.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/image_candidates.py backend/app/main.py backend/tests/integration/conftest.py backend/tests/integration/test_image_candidates_api.py
git commit -m "feat(image-candidates): 创建/删除候选端点（multipart 支持临时参考图上传）"
```

---

### Task 7: API — 采纳候选（三种 slot）

**Files:**
- Modify: `backend/app/services/first_frame.py`（迁入 `_propagate_first_frame_to_next` → 改名 `propagate_first_frame_to_next`）
- Modify: `backend/worker/tasks.py:579-610`（删除原函数，改为 import）
- Modify: `backend/app/api/image_candidates.py`（加 adopt 端点）
- Test: `backend/tests/integration/test_image_candidate_adopt.py`

**Interfaces:**
- Produces:
  - `POST /api/projects/{pid}/shots/{sid}/image-candidates/{cid}/adopt` → 按 slot 复制入槽，返回 `{"shot_id", "slot", "candidate": {...}, "custom_first_frame_path"|"target_last_frame_path"|"last_frame_path": <media url>}`。
  - `first_frame.propagate_first_frame_to_next(project_id, shot, last_frame_path: str, session)` — 与原 worker 内 `_propagate_first_frame_to_next`（tasks.py:579）逐行相同，仅移动位置+去下划线。
- 采纳语义（素材审计核心，来自 spec）：
  - `first_frame`：复制 → `shot_custom_frames_dir/{ts_uuid}` → 写 `custom_first_frame_path`。
  - `tail_frame`：复制 → `shot_dir/{ts_uuid}` → 写 `target_last_frame_path` + `tf_status="done"`。
  - `cc`：复制 → `shot_dir/cc_{ts_uuid}.png`（与现有 CC 命名一致，保住 `pristine_last_frame_path`/revert 链）；先删其他 `cc_*.png`；写 `last_frame_path` + `cc_status="done"` + `cc_error_message=None`；调 `propagate_first_frame_to_next`。
  - 同 shot 同 slot 其他候选 `adopted_at` 清空；本候选置 `datetime.utcnow()`。
  - 候选 status != done → 400。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/integration/test_image_candidate_adopt.py
"""采纳候选：三种 slot 的写槽语义（复制而非移动；素材审计）."""
import pytest
from datetime import datetime
from pathlib import Path
from sqlalchemy import select

from tests.integration.conftest import HEADERS, _make_project, _add_shot
from app.models.project import ImageCandidate, Shot
from app.services.storage import shot_dir


async def _seed_done_candidate(sf, tmp_path_factory, pid, shot_id, slot, data=b"IMG"):
    f = tmp_path_factory / f"cand_{slot}.png"
    f.write_bytes(data)
    async with sf() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == shot_id)
        )).scalar_one()
        c = ImageCandidate(
            project_id=pid, shot_pk=shot.id, shot_id=shot_id, slot=slot,
            status="done", file_path=str(f),
        )
        s.add(c)
        await s.commit()
        await s.refresh(c)
        return c.id, f


async def _adopt(client, pid, shot_id, cid):
    return await client.post(
        f"/api/projects/{pid}/shots/{shot_id}/image-candidates/{cid}/adopt",
        headers=HEADERS,
    )


async def test_adopt_first_frame_copies_into_custom_frames(client, db_session_factory, tmp_path):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    cid, src = await _seed_done_candidate(db_session_factory, tmp_path, pid, 1, "first_frame")

    r = await _adopt(client, pid, 1, cid)
    assert r.status_code == 200

    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        assert shot.custom_first_frame_path is not None
        assert "custom_frames" in shot.custom_first_frame_path
        assert Path(shot.custom_first_frame_path).read_bytes() == b"IMG"
        assert shot.custom_first_frame_path != str(src)
    assert src.exists()  # 复制而非移动：候选原件不动


async def test_adopt_tail_frame_sets_path_and_tf_status(client, db_session_factory, tmp_path):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    cid, src = await _seed_done_candidate(db_session_factory, tmp_path, pid, 1, "tail_frame")

    r = await _adopt(client, pid, 1, cid)
    assert r.status_code == 200

    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        assert shot.target_last_frame_path is not None
        assert shot.tf_status == "done"
        assert Path(shot.target_last_frame_path).read_bytes() == b"IMG"
    assert src.exists()


async def test_adopt_cc_replaces_last_frame_and_propagates(client, db_session_factory, tmp_path):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await _add_shot(db_session_factory, pid, 2)

    # shot1 有 last_frame + 旧 cc 文件；shot2 未生成（可被连贯链更新）
    s_dir = shot_dir(pid, 1); s_dir.mkdir(parents=True, exist_ok=True)
    lf = s_dir / "last_frame_1_aaaa.png"; lf.write_bytes(b"OLD")
    old_cc = s_dir / "cc_0_bbbb.png"; old_cc.write_bytes(b"OLDCC")
    async with db_session_factory() as s:
        shot1 = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot1.last_frame_path = str(old_cc)
        await s.commit()

    cid, _ = await _seed_done_candidate(db_session_factory, tmp_path, pid, 1, "cc", data=b"NEWCC")
    r = await _adopt(client, pid, 1, cid)
    assert r.status_code == 200

    async with db_session_factory() as s:
        shot1 = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot2 = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 2)
        )).scalar_one()
        assert shot1.cc_status == "done"
        p = Path(shot1.last_frame_path)
        assert p.name.startswith("cc_") and p.read_bytes() == b"NEWCC"
        assert not old_cc.exists()          # 旧校准帧被清
        assert lf.exists()                  # pristine 不动（revert 链保住）
        assert shot2.custom_first_frame_path == shot1.last_frame_path  # 连贯链传播


async def test_adopt_exclusive_per_slot_and_requires_done(client, db_session_factory, tmp_path):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    cid1, _ = await _seed_done_candidate(db_session_factory, tmp_path, pid, 1, "tail_frame", b"A")
    cid2, _ = await _seed_done_candidate(db_session_factory, tmp_path, pid, 1, "tail_frame", b"B")

    assert (await _adopt(client, pid, 1, cid1)).status_code == 200
    assert (await _adopt(client, pid, 1, cid2)).status_code == 200

    async with db_session_factory() as s:
        rows = (await s.execute(select(ImageCandidate))).scalars().all()
        adopted = {c.id: c.adopted_at for c in rows}
        assert adopted[cid2] is not None and adopted[cid1] is None  # 同槽位互斥

    # 未完成候选不可采纳
    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        c = ImageCandidate(project_id=pid, shot_pk=shot.id, shot_id=1, slot="tail_frame")
        s.add(c); await s.commit(); await s.refresh(c)
        pending = c.id
    assert (await _adopt(client, pid, 1, pending)).status_code == 400
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/integration/test_image_candidate_adopt.py -v`
Expected: FAIL（404 adopt 路由不存在）

- [ ] **Step 3: 迁移 propagate + 实现 adopt**

`backend/app/services/first_frame.py` 末尾加（正文=tasks.py:579-610 原函数逐行拷贝）：

```python
async def propagate_first_frame_to_next(
    project_id: str, shot: Shot, last_frame_path: str, session: AsyncSession
) -> None:
    """Shot N 的 last_frame 变更后，把它写入下一连续 shot 的 custom_first_frame_path。

    仅当下一镜未生成视频、且其首帧不是用户手动覆盖（custom_frames/ 路径）时更新。
    （原 worker/tasks.py:_propagate_first_frame_to_next，逐行迁移。）
    """
    next_result = await session.execute(
        select(Shot).where(
            Shot.project_id == project_id, Shot.shot_id == shot.shot_id + 1
        )
    )
    next_shot = next_result.scalar_one_or_none()
    if not next_shot or not next_shot.use_prev_last_frame:
        return
    if next_shot.video_path:
        return
    existing = next_shot.custom_first_frame_path
    is_user_override = bool(existing) and "custom_frames" in existing
    if not is_user_override and existing != last_frame_path:
        next_shot.custom_first_frame_path = last_frame_path
        session.add(next_shot)
```

> 迁移时以 `worker/tasks.py:579-610` 现行代码为准逐行拷贝（上面已含其全部分支），删除原函数，`tasks.py` 原调用点（:1166 CC 内、及 grep 到的其它调用）改为 `from app.services.first_frame import propagate_first_frame_to_next`。用 `grep -n "_propagate_first_frame_to_next" backend/worker/tasks.py` 找全调用点。

`image_candidates.py` 加 adopt 端点：

```python
@router.post("/projects/{project_id}/shots/{shot_id}/image-candidates/{candidate_id}/adopt")
async def adopt_image_candidate(
    project_id: str,
    shot_id: int,
    candidate_id: str,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """采纳候选：复制入槽（路径即真相），同槽位其他候选取消采纳标记。"""
    import shutil
    from app.api.projects import _candidate_to_dict
    from app.services.first_frame import propagate_first_frame_to_next
    from app.services.storage import shot_custom_frames_dir, shot_dir

    await _get_project_or_404(project_id, session)
    shot = await _get_shot_or_404(project_id, shot_id, session)
    cand = await _get_candidate_or_404(project_id, shot_id, candidate_id, session)

    if cand.status != "done" or not cand.file_path or not Path(cand.file_path).exists():
        raise HTTPException(status_code=400, detail="Candidate is not ready to adopt")

    src = Path(cand.file_path)
    extra: dict = {}

    if cand.slot == "first_frame":
        dest_dir = shot_custom_frames_dir(project_id, shot_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / ts_uuid_name(src.suffix or ".png")
        shutil.copy2(src, dest)
        shot.custom_first_frame_path = str(dest)
        extra["custom_first_frame_path"] = to_media_url(str(dest))
    elif cand.slot == "tail_frame":
        dest_dir = shot_dir(project_id, shot_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / ts_uuid_name(src.suffix or ".png")
        shutil.copy2(src, dest)
        shot.target_last_frame_path = str(dest)
        shot.tf_status = "done"
        extra["target_last_frame_path"] = to_media_url(str(dest))
    elif cand.slot == "cc":
        s_dir = shot_dir(project_id, shot_id)
        s_dir.mkdir(parents=True, exist_ok=True)
        dest = s_dir / f"cc_{ts_uuid_name('.png')}"
        shutil.copy2(src, dest)
        # 旧校准帧只留最新（沿用原 CC worker 行为）；pristine last_frame_* 不动
        for _old in s_dir.glob("cc_*.png"):
            if _old != dest:
                _old.unlink(missing_ok=True)
        shot.last_frame_path = str(dest)
        shot.cc_status = "done"
        shot.cc_error_message = None
        await propagate_first_frame_to_next(project_id, shot, str(dest), session)
        extra["last_frame_path"] = to_media_url(str(dest))
    else:
        raise HTTPException(status_code=400, detail=f"Unknown slot: {cand.slot}")

    # 同槽位互斥采纳标记
    siblings = (await session.execute(
        select(ImageCandidate).where(
            ImageCandidate.project_id == project_id,
            ImageCandidate.shot_id == shot_id,
            ImageCandidate.slot == cand.slot,
        )
    )).scalars().all()
    for c in siblings:
        c.adopted_at = None
        session.add(c)
    cand.adopted_at = datetime.utcnow()
    session.add_all([cand, shot])
    await session.commit()
    await session.refresh(cand)

    return {"shot_id": shot_id, "slot": cand.slot, "candidate": _candidate_to_dict(cand), **extra}
```

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `uv run pytest tests/integration/test_image_candidate_adopt.py tests/integration/test_image_candidates_api.py -v && uv run pytest tests/integration -x -q`
Expected: PASS（含 CC worker 原 propagate 调用点回归）

- [ ] **Step 5: Commit**

```bash
git add -A backend
git commit -m "feat(image-candidates): 采纳端点（三 slot 复制入槽 + 连贯链传播迁至服务层）"
```

---

### Task 8: Worker — `run_image_candidate`

**Files:**
- Modify: `backend/worker/tasks.py`（新增任务函数；放在 `run_tail_frame_pipeline` 前）
- Modify: `backend/worker/arq_worker.py:9-17,70-78`（注册）
- Test: `backend/tests/integration/test_run_image_candidate.py`

**Interfaces:**
- Produces: `run_image_candidate(ctx, project_id: str, shot_id: int, candidate_id: str, actor: str)`。
- 模式解析：`slot=="cc"` → cc_edit；`custom_prompt` 非空 → custom；否则 slot 方向自动推理。
- 参考图：`ref_paths` JSON 里**键存在即显式**（含空数组）；无 ref_paths 或缺 `character` 键 → `_get_character_ref_paths` 默认。
- context 帧：tail 模式 = `pick_first_frame`；first/custom-first 模式 = `resolve_tail_frame(target_last_frame_path)` → 存在的 `last_frame_path` → None。
- 事件：`image_candidate_started` / `image_candidate_completed`（带 `file_path` media url）/ `image_candidate_failed`；auto tail 保留 `tf_pose_analyzed`。
- 不触碰 project 状态机；auto tail 生成的 `motion_prompt` 仍回写 shot（沿用旧行为）。
- Consumes: Tasks 2-5 的 `generate_tail_frame/generate_first_frame/generate_custom/calibrate_face`，Task 1 的 `ImageCandidate/shot_candidates_dir`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/integration/test_run_image_candidate.py
"""run_image_candidate worker：模式路由、候选状态流转、事件（mock 全部生成函数）."""
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from sqlalchemy import select

from tests.integration.conftest import _make_project, _add_shot, _add_character_image
from app.models.project import ImageCandidate, Shot


@pytest.fixture
async def worker_ctx(db_session_factory, redis):
    return {"session_factory": db_session_factory, "redis": redis}


async def _seed_candidate(sf, pid, shot_id=1, slot="tail_frame", custom_prompt=None, ref_paths=None):
    async with sf() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == shot_id)
        )).scalar_one()
        c = ImageCandidate(
            project_id=pid, shot_pk=shot.id, shot_id=shot_id, slot=slot,
            custom_prompt=custom_prompt,
            prompt_source="custom" if custom_prompt else "auto",
            ref_paths=ref_paths,
        )
        s.add(c); await s.commit(); await s.refresh(c)
        return c.id


def _fake_gen(out_bytes=b"GEN"):
    async def _fake(*args, **kwargs):
        out = kwargs.get("output_path") or args[-1]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(out_bytes)
        return out
    return _fake


async def test_auto_tail_uses_generate_tail_frame(worker_ctx, db_session_factory, monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    from worker.tasks import run_image_candidate

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await _add_character_image(db_session_factory, pid)
    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        shot.motion_prompt = "walk forward"
        await s.commit()
    cid = await _seed_candidate(db_session_factory, pid)

    with patch("app.services.image_generation.generate_tail_frame", new=AsyncMock(side_effect=_fake_gen())) as gtf, \
         patch("worker.tasks.pick_first_frame", new=AsyncMock(return_value=None)):
        await run_image_candidate(worker_ctx, pid, 1, cid, "user:test")

    gtf.assert_awaited_once()
    assert gtf.await_args.kwargs["motion_prompt"] == "walk forward"
    async with db_session_factory() as s:
        cand = (await s.execute(select(ImageCandidate).where(ImageCandidate.id == cid))).scalar_one()
        assert cand.status == "done"
        assert "candidates" in cand.file_path
        assert Path(cand.file_path).read_bytes() == b"GEN"


async def test_custom_prompt_routes_to_generate_custom(worker_ctx, db_session_factory, monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    from worker.tasks import run_image_candidate

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await _add_character_image(db_session_factory, pid)
    cid = await _seed_candidate(
        db_session_factory, pid, slot="first_frame", custom_prompt="my prompt",
        ref_paths=json.dumps({"character": [], "object": []}),
    )

    with patch("app.services.image_generation.generate_custom", new=AsyncMock(side_effect=_fake_gen())) as gc:
        await run_image_candidate(worker_ctx, pid, 1, cid, "user:test")

    gc.assert_awaited_once()
    kw = gc.await_args.kwargs
    assert kw["prompt"] == "my prompt"
    assert kw["character_ref_paths"] == []   # 显式空列表不回退默认


async def test_cc_slot_routes_to_calibrate_face(worker_ctx, db_session_factory, monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    from worker.tasks import run_image_candidate
    from app.services.storage import shot_dir

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await _add_character_image(db_session_factory, pid)
    s_dir = shot_dir(pid, 1); s_dir.mkdir(parents=True, exist_ok=True)
    lf = s_dir / "last_frame_1_aaaa.png"; lf.write_bytes(b"LF")
    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        shot.last_frame_path = str(lf)
        await s.commit()
    cid = await _seed_candidate(db_session_factory, pid, slot="cc")

    with patch("app.services.image_generation.calibrate_face", new=AsyncMock(side_effect=_fake_gen(b"CC"))) as cf:
        await run_image_candidate(worker_ctx, pid, 1, cid, "user:test")

    cf.assert_awaited_once()
    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        cand = (await s.execute(select(ImageCandidate).where(ImageCandidate.id == cid))).scalar_one()
        assert cand.status == "done"
        assert shot.last_frame_path == str(lf)  # CC 候选化：不直写 last_frame


async def test_failure_marks_candidate_failed_only(worker_ctx, db_session_factory, monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    from worker.tasks import run_image_candidate
    from app.models.project import Project

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await _add_character_image(db_session_factory, pid)
    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        shot.motion_prompt = "m"
        await s.commit()
    cid = await _seed_candidate(db_session_factory, pid)

    with patch("app.services.image_generation.generate_tail_frame", new=AsyncMock(side_effect=RuntimeError("blocked"))), \
         patch("worker.tasks.pick_first_frame", new=AsyncMock(return_value=None)):
        await run_image_candidate(worker_ctx, pid, 1, cid, "user:test")

    async with db_session_factory() as s:
        cand = (await s.execute(select(ImageCandidate).where(ImageCandidate.id == cid))).scalar_one()
        proj = (await s.execute(select(Project).where(Project.id == pid))).scalar_one()
        assert cand.status == "failed" and "blocked" in cand.error
        assert proj.status == "shot_review"  # project 状态机不受影响
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/integration/test_run_image_candidate.py -v`
Expected: FAIL `ImportError: cannot import name 'run_image_candidate'`

- [ ] **Step 3: 实现**

`backend/worker/tasks.py` 在 `run_tail_frame_pipeline` 前插入（imports 顶部补 `from app.models.project import ImageCandidate` 与 `from app.services.storage import shot_candidates_dir`——并入现有 import 块）：

```python
def _resolve_ff_context(shot: Shot) -> str | None:
    """first_frame/custom-first 的 context 帧：目标尾帧 → 实际尾帧 → 无。"""
    ctx = resolve_tail_frame(shot.target_last_frame_path)
    if ctx:
        return ctx
    if shot.last_frame_path and Path(shot.last_frame_path).exists():
        return shot.last_frame_path
    return None


@observability.traced_job("worker-image-candidate-run", tags=["image-candidate"])
async def run_image_candidate(
    ctx: Dict[str, Any], project_id: str, shot_id: int, candidate_id: str, actor: str
) -> None:
    """统一图片候选生成：模式 = slot + 有无 custom_prompt；不触碰 project 状态机。"""
    from app.services import image_generation as ig

    worker_ctx = WorkerContext(ctx)
    session_factory = worker_ctx.session_factory
    redis = worker_ctx.redis

    async with session_factory() as session:
        project = (await session.execute(
            select(Project).where(Project.id == project_id)
        )).scalar_one_or_none()
        shot = (await session.execute(
            select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
        )).scalar_one_or_none()
        cand = (await session.execute(
            select(ImageCandidate).where(ImageCandidate.id == candidate_id)
        )).scalar_one_or_none()
        if not project or not shot or not cand:
            logger.error("run_image_candidate: missing project/shot/candidate (%s/%s/%s)",
                         project_id, shot_id, candidate_id)
            return

        await publish_event(redis, project_id, {
            "type": "image_candidate_started",
            "data": {"shot_id": shot_id, "candidate_id": candidate_id, "slot": cand.slot},
        })

        try:
            refs = json.loads(cand.ref_paths) if cand.ref_paths else {}
            if "character" in refs:
                char_refs = refs["character"]
            else:
                char_refs = await _get_character_ref_paths(project_id, session)
            obj_refs = refs.get("object") or None

            out_dir = shot_candidates_dir(project_id, shot_id)
            out_dir.mkdir(parents=True, exist_ok=True)
            out = str(out_dir / ts_uuid_name(".png"))

            if cand.slot == "cc":
                pristine = pristine_last_frame_path(project_id, shot_id)
                if pristine is None and shot.last_frame_path and Path(shot.last_frame_path).exists():
                    pristine = Path(shot.last_frame_path)
                if pristine is None:
                    raise ValueError("Shot has no last frame to calibrate")
                await ig.calibrate_face(char_refs, str(pristine), out)

            elif cand.custom_prompt:
                if cand.slot == "tail_frame":
                    ff = await pick_first_frame(project_id, shot, session)
                    context = str(ff) if ff else None
                else:
                    context = _resolve_ff_context(shot)
                await ig.generate_custom(
                    prompt=cand.custom_prompt,
                    output_path=out,
                    character_ref_paths=char_refs,
                    object_ref_paths=obj_refs,
                    context_frame_path=context,
                    aspect_ratio=project.aspect_ratio,
                )

            elif cand.slot == "tail_frame":
                first_frame = await pick_first_frame(project_id, shot, session)
                if shot.motion_prompt and not obj_refs:
                    motion_prompt = shot.motion_prompt
                else:
                    motion_prompt = await run_director_agent(
                        shot_id=shot.shot_id,
                        shot_type=shot.shot_type,
                        visual_description=shot.visual_description,
                        text=shot.text,
                        duration=shot.shot_duration,
                        llm_provider=get_provider(),
                        reference_image_paths=obj_refs,
                    )
                    shot.motion_prompt = motion_prompt
                    session.add(shot)
                    await session.commit()

                async def _on_cot(end_pose: str) -> None:
                    await publish_event(redis, project_id, {
                        "type": "tf_pose_analyzed",
                        "data": {"shot_id": shot_id, "end_pose": end_pose},
                    })

                await ig.generate_tail_frame(
                    character_ref_paths=char_refs,
                    first_frame_path=str(first_frame) if first_frame else None,
                    motion_prompt=motion_prompt,
                    output_path=out,
                    object_ref_paths=obj_refs,
                    aspect_ratio=project.aspect_ratio,
                    on_cot_complete=_on_cot,
                )

            else:  # first_frame auto
                await ig.generate_first_frame(
                    character_ref_paths=char_refs,
                    context_frame_path=_resolve_ff_context(shot),
                    visual_description=shot.visual_description,
                    shot_type=shot.shot_type,
                    output_path=out,
                    motion_prompt=shot.motion_prompt,
                    object_ref_paths=obj_refs,
                    aspect_ratio=project.aspect_ratio,
                )

            cand.file_path = out
            cand.status = "done"
            cand.error = None
            if cand.slot == "cc":
                shot.cc_status = None
                shot.cc_error_message = None
                session.add(shot)
            session.add(cand)
            await session.commit()
            await publish_event(redis, project_id, {
                "type": "image_candidate_completed",
                "data": {
                    "shot_id": shot_id,
                    "candidate_id": candidate_id,
                    "slot": cand.slot,
                    "file_path": to_media_url(out),
                },
            })
            logger.info("Image candidate %s done (slot=%s shot=%d)", candidate_id, cand.slot, shot_id)

        except Exception as e:
            logger.error("Image candidate %s failed: %s", candidate_id, e, exc_info=True)
            cand.status = "failed"
            cand.error = str(e)
            if cand.slot == "cc":
                shot.cc_status = "failed"
                shot.cc_error_message = str(e)
                session.add(shot)
            session.add(cand)
            await session.commit()
            await publish_event(redis, project_id, {
                "type": "image_candidate_failed",
                "data": {
                    "shot_id": shot_id,
                    "candidate_id": candidate_id,
                    "slot": cand.slot,
                    "error_message": str(e),
                },
            })
```

> 注意 mock 边界：测试 patch 的是 `app.services.image_generation.generate_*`，因此 worker 内必须 `from app.services import image_generation as ig` 然后 `ig.generate_tail_frame(...)`（模块属性访问），不能 `from ... import generate_tail_frame` 到本地名。

`backend/worker/arq_worker.py` import 与 `functions` 列表各加 `run_image_candidate`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_run_image_candidate.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/worker/tasks.py backend/worker/arq_worker.py backend/tests/integration/test_run_image_candidate.py
git commit -m "feat(image-candidates): run_image_candidate 统一 worker（四模式路由 + 事件）"
```

---

### Task 9: 旧生成端点收敛为候选 wrapper，删除旧 pipeline

**Files:**
- Modify: `backend/app/api/pipeline.py:903-937`（generate-tail-frame）、`:940-980`（generate-first-frame）
- Modify: `backend/worker/tasks.py`（删 `run_tail_frame_pipeline` :628-775 与 `run_first_frame_pipeline` :777-880）
- Modify: `backend/worker/arq_worker.py`（去注册）
- Modify: `backend/tests/integration/test_tail_frame_pipeline.py`、`test_first_frame_pipeline.py`、`test_regenerate_tail_frame.py`（如引用）
- Test: 上述改造后的集成测试

**Interfaces:**
- Produces: 两个旧端点保持 URL/202/`{"status":"queued","shot_id"}` 形状（新增 `candidate_id` 字段），内部 = 建 auto 候选 + `enqueue_job("run_image_candidate", ...)`。**不再**做 project 状态机 transition、不再写 `tf_status/ff_status`、不再调 `_reset_tail_frame`。
- `run_tail_frame_pipeline` / `run_first_frame_pipeline` 从代码库消失。

- [ ] **Step 1: 更新集成测试（先红）**

改 `test_tail_frame_pipeline.py` 的 generate-tail-frame 三个用例：

```python
async def test_generate_tail_frame_success(client, db_session_factory):
    """generate-tail-frame 现在创建 auto 候选并入队 run_image_candidate."""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="pending")

    r = await client.post(
        f"/api/projects/{pid}/shots/1/generate-tail-frame", headers=HEADERS
    )
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    cid = body["candidate_id"]

    client.arq.enqueue_job.assert_called_once_with(
        "run_image_candidate", pid, 1, cid, f"user:{USER}"
    )
    from app.models.project import ImageCandidate
    async with db_session_factory() as s:
        cand = (await s.execute(
            select(ImageCandidate).where(ImageCandidate.id == cid)
        )).scalar_one()
        assert cand.slot == "tail_frame" and cand.prompt_source == "auto"
```

`test_generate_tail_frame_wrong_status`：改为期望 **202**（候选生成不再要求状态机 transition），重命名为 `test_generate_tail_frame_any_status_ok`。`shot_not_found` 用例保持 404 不变。`test_first_frame_pipeline.py` 的 generate-first-frame 端点用例做同构修改（slot="first_frame"）；该文件里直接调用 `run_first_frame_pipeline` 的 worker 用例改为调 `run_image_candidate`（构造 first_frame auto 候选后调用，断言与 Task 8 测试同构）或删除重复覆盖。

Run: `uv run pytest tests/integration/test_tail_frame_pipeline.py tests/integration/test_first_frame_pipeline.py -v`
Expected: 新断言 FAIL（旧实现仍入队 run_tail_frame_pipeline）

- [ ] **Step 2: 改端点实现**

`pipeline.py` generate_tail_frame 端点函数体替换为：

```python
    """[Deprecated wrapper] 创建 auto 尾帧候选（新入口：POST .../image-candidates）。"""
    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")

    from app.models.project import ImageCandidate
    cand = ImageCandidate(
        project_id=project_id, shot_pk=shot.id, shot_id=shot_id,
        slot="tail_frame", status="generating", prompt_source="auto",
    )
    session.add(cand)
    await session.commit()
    await session.refresh(cand)

    arq = await _get_arq_redis(redis)
    await arq.enqueue_job("run_image_candidate", project_id, shot_id, cand.id, f"user:{user}")
    return {"status": "queued", "shot_id": shot_id, "candidate_id": cand.id}
```

generate_first_frame 端点同构（slot="first_frame"，去掉原 ff_status 写入逻辑）。

- [ ] **Step 3: 删除旧 pipeline**

删 `worker/tasks.py` 的 `run_tail_frame_pipeline` 与 `run_first_frame_pipeline` 整函数；`arq_worker.py` 的 import 与 `functions` 列表去掉两者。`grep -rn "run_tail_frame_pipeline\|run_first_frame_pipeline" backend/ frontend-vite/src tests/` 确认无残留引用（e2e mock 的 URL 不受影响）。

- [ ] **Step 4: 回归**

Run: `uv run pytest tests/integration -q && uv run pytest tests/unit -q`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add -A backend
git commit -m "refactor(image-candidates): 旧 generate-tail/first-frame 端点收敛为候选 wrapper，删除旧 pipeline"
```

---

### Task 10: CC 候选化（校准 worker 产候选，不直写）

**Files:**
- Modify: `backend/worker/tasks.py:1123-1190`（`_do_character_calibrate_one` 重写）
- Test: `backend/tests/integration/test_cc_candidates.py`

**Interfaces:**
- Produces: `_do_character_calibrate_one` 行为变更 —— 建 `ImageCandidate(slot="cc")` → 生成到 candidates 目录 → `status="done"` → 发 `cc_candidate_ready` 事件；**不再**写 `shot.last_frame_path`、不再删旧 `cc_*.png`、不再 propagate（这些移到了 adopt，Task 7）。失败时候选 failed + `cc_status="failed"`（沿用 `_mark_shot_failed`）。
- `run_character_calibrate` / `run_character_calibrate_batch` / 校准 API 端点签名与事件（`cc_started`/`cc_batch_done`）不变。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/integration/test_cc_candidates.py
"""CC 候选化：校准产候选，不直写 last_frame；失败标记 cc_status."""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from sqlalchemy import select

from tests.integration.conftest import _make_project, _add_shot, _add_character_image
from app.models.project import ImageCandidate, Shot
from app.services.storage import shot_dir


async def _seed_shot_with_last_frame(sf, monkeypatch, tmp_path, pid):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    s_dir = shot_dir(pid, 1); s_dir.mkdir(parents=True, exist_ok=True)
    lf = s_dir / "last_frame_1_aaaa.png"; lf.write_bytes(b"LF")
    async with sf() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.last_frame_path = str(lf)
        await s.commit()
    return lf


async def test_calibrate_creates_candidate_not_replace(db_session_factory, redis, monkeypatch, tmp_path):
    from worker.tasks import _do_character_calibrate_one

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    lf = await _seed_shot_with_last_frame(db_session_factory, monkeypatch, tmp_path, pid)

    async def _fake_cc(refs, src, out):
        Path(out).write_bytes(b"CC")
        return out

    with patch("app.services.image_generation.calibrate_face", new=AsyncMock(side_effect=_fake_cc)):
        await _do_character_calibrate_one(
            db_session_factory, redis, pid, 1, ["/fake/ref.jpg"]
        )

    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        cand = (await s.execute(select(ImageCandidate))).scalar_one()
        assert shot.last_frame_path == str(lf)          # 不直写
        assert shot.cc_status is None
        assert cand.slot == "cc" and cand.status == "done"
        assert "candidates" in cand.file_path
        assert Path(cand.file_path).read_bytes() == b"CC"


async def test_calibrate_failure_marks_candidate_and_cc_status(db_session_factory, redis, monkeypatch, tmp_path):
    from worker.tasks import _do_character_calibrate_one

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await _seed_shot_with_last_frame(db_session_factory, monkeypatch, tmp_path, pid)

    with patch("app.services.image_generation.calibrate_face", new=AsyncMock(side_effect=RuntimeError("boom"))):
        with pytest.raises(RuntimeError):
            await _do_character_calibrate_one(
                db_session_factory, redis, pid, 1, ["/fake/ref.jpg"]
            )

    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalar_one()
        cand = (await s.execute(select(ImageCandidate))).scalar_one()
        assert cand.status == "failed" and "boom" in cand.error
        assert shot.cc_status == "failed"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/integration/test_cc_candidates.py -v`
Expected: FAIL（旧实现直写 last_frame，且无候选行）

- [ ] **Step 3: 重写 `_do_character_calibrate_one`**

```python
async def _do_character_calibrate_one(
    session_factory,
    redis,
    project_id: str,
    shot_id: int,
    ref_image_paths: list[str],
) -> None:
    """校准一个 shot 的 last frame → 产出 cc 候选（采纳后才替换，见 adopt 端点）。"""
    from app.services import image_generation as ig

    async with session_factory() as session:
        result = await session.execute(
            select(Shot).where(
                Shot.project_id == project_id, Shot.shot_id == shot_id
            )
        )
        shot = result.scalar_one_or_none()
        if not shot or not shot.last_frame_path:
            raise ValueError(f"Shot {shot_id} not found or has no last frame")

        await publish_event(
            redis, project_id,
            {"type": "cc_started", "data": {"shot_id": shot_id}},
        )

        cand = ImageCandidate(
            project_id=project_id, shot_pk=shot.id, shot_id=shot_id,
            slot="cc", status="generating", prompt_source="auto",
            ref_paths=json.dumps({"character": ref_image_paths}),
        )
        session.add(cand)
        await session.commit()
        await session.refresh(cand)

        try:
            # 从未校准的 pristine 帧出发（绝不叠加已校准帧）
            pristine = pristine_last_frame_path(project_id, shot_id) or Path(shot.last_frame_path)
            out_dir = shot_candidates_dir(project_id, shot_id)
            out_dir.mkdir(parents=True, exist_ok=True)
            out = str(out_dir / ts_uuid_name(".png"))
            await ig.calibrate_face(ref_image_paths, str(pristine), out)

            cand.file_path = out
            cand.status = "done"
            shot.cc_status = None
            shot.cc_error_message = None
            session.add_all([cand, shot])
            await session.commit()

            await publish_event(
                redis, project_id,
                {
                    "type": "cc_candidate_ready",
                    "data": {
                        "shot_id": shot_id,
                        "candidate_id": cand.id,
                        "file_path": to_media_url(out),
                    },
                },
            )
            logger.info("CC candidate ready for shot %d", shot_id)

        except Exception as e:
            logger.error("Character calibration failed for shot %d: %s", shot_id, e)
            cand.status = "failed"
            cand.error = str(e)
            session.add(cand)
            await session.commit()
            await _mark_shot_failed(
                session, redis, project_id, shot, e,
                status_field="cc_status", status_value="failed",
                error_field="cc_error_message", event_type="cc_failed",
                shot_id=shot_id,
            )
            raise
```

（原函数里的 `cc_completed` 事件与 `_propagate_first_frame_to_next` 调用随之消失——propagate 已在 Task 7 的 adopt 里。）

- [ ] **Step 4: 回归（含 revert 端点与批量）**

Run: `uv run pytest tests/integration/test_cc_candidates.py -v && uv run pytest tests/integration tests/unit -q`
Expected: PASS。若有旧测试断言"校准后 last_frame 被替换"，按新语义改为断言候选产生（用 `grep -rn "cc_completed\|calibrate" backend/tests/` 找全）。

- [ ] **Step 5: Commit**

```bash
git add -A backend
git commit -m "feat(cc): 人脸校准候选化 — 产 cc 候选待采纳，不再直写 last_frame"
```

---

### Task 11: 前端 types + api + SSE 事件

**Files:**
- Modify: `frontend-vite/src/lib/types.ts:58-95`（`ImageCandidate` + `Shot.image_candidates`）
- Modify: `frontend-vite/src/lib/api.ts`（`uploadForm` helper + 三个方法）
- Modify: `frontend-vite/src/pages/ShotsPage.tsx:119` 附近（SSE 事件 → 刷新项目）
- Test: `cd frontend-vite && npx tsc -b`（类型检查）

**Interfaces:**
- Produces（`types.ts`）：

```typescript
export type ImageCandidateSlot = 'first_frame' | 'tail_frame' | 'cc'
export type ImageCandidateStatus = 'generating' | 'done' | 'failed'

export interface ImageCandidate {
  id: string
  shot_id: number
  slot: ImageCandidateSlot
  status: ImageCandidateStatus
  file_path: string | null
  prompt_source: 'auto' | 'custom'
  custom_prompt: string | null
  error: string | null
  created_at: string
  adopted_at: string | null
}
```

`Shot` interface 加一行 `image_candidates: ImageCandidate[]`。

- Produces（`api.ts`，加在 `characterCalibrateRevert` 后）：

```typescript
export interface CreateImageCandidateOpts {
  slot: 'first_frame' | 'tail_frame'
  customPrompt?: string
  refImageIds?: string[]
  includeShotRefs?: boolean
  files?: File[]
}
```

```typescript
  // ── 统一图片生成：候选画廊 ──

  // 创建候选（202；multipart，自定义提示词/参考图勾选/临时上传均可选）
  createImageCandidate: (
    projectId: string, shotId: number, opts: CreateImageCandidateOpts
  ): Promise<{ status: string; candidate: ImageCandidate }> => {
    const form = new FormData()
    form.append('slot', opts.slot)
    if (opts.customPrompt?.trim()) form.append('custom_prompt', opts.customPrompt.trim())
    if (opts.refImageIds) form.append('ref_image_ids', JSON.stringify(opts.refImageIds))
    if (opts.includeShotRefs !== undefined) form.append('include_shot_refs', String(opts.includeShotRefs))
    for (const f of opts.files ?? []) form.append('files', f)
    return uploadForm(`/api/projects/${projectId}/shots/${shotId}/image-candidates`, form)
  },

  // 采纳候选 → 写入槽位
  adoptImageCandidate: (
    projectId: string, shotId: number, candidateId: string
  ): Promise<{ shot_id: number; slot: string; candidate: ImageCandidate }> => {
    return request('POST', `/api/projects/${projectId}/shots/${shotId}/image-candidates/${candidateId}/adopt`)
  },

  // 删除候选
  deleteImageCandidate: (
    projectId: string, shotId: number, candidateId: string
  ): Promise<{ deleted: string }> => {
    return request('DELETE', `/api/projects/${projectId}/shots/${shotId}/image-candidates/${candidateId}`)
  },
```

`uploadForm` helper（放 `uploadSingle`（:83）旁，同其错误处理风格）：

```typescript
async function uploadForm<T>(path: string, form: FormData): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'X-User-Name': getUserName() },
    body: form,
  })
  if (!response.ok) {
    const detail = await response.text()
    throw new APIErrorClass({ code: String(response.status), message: detail })
  }
  return response.json()
}
```

（import `ImageCandidate` 类型进 api.ts。）

- SSE：`ShotsPage.tsx` 处理 `tf_completed` 的 switch/if 处（:119 附近）加上对 `image_candidate_started` / `image_candidate_completed` / `image_candidate_failed` / `cc_candidate_ready` 的分支，行为 = 触发现有的"重新拉取项目详情"逻辑（与 `tf_completed` 同一刷新函数）。

- [ ] **Step 1: 实现上述三处**
- [ ] **Step 2: 类型检查**

Run: `cd frontend-vite && npx tsc -b`
Expected: 无错误

- [ ] **Step 3: Commit**

```bash
git add frontend-vite/src/lib/types.ts frontend-vite/src/lib/api.ts frontend-vite/src/pages/ShotsPage.tsx
git commit -m "feat(frontend): ImageCandidate 类型 + 候选 API + SSE 刷新"
```

---

### Task 12: 前端 — GenerateImageDialog + 入口接线

**Files:**
- Create: `frontend-vite/src/components/GenerateImageDialog.tsx`
- Modify: `frontend-vite/src/components/ShotCard.tsx:922-945`（菜单项 onClick 改为打开弹窗）
- Modify: `frontend-vite/src/pages/ShotsPage.tsx`（弹窗 state + 渲染）
- Test: `npx tsc -b` + 手动冒烟（见 Step 4）

**Interfaces:**
- Produces: `<GenerateImageDialog project={ProjectDetail} shot={Shot} slot={'first_frame'|'tail_frame'} open onOpenChange onChanged={() => void} />` — onChanged = 采纳/删除/生成后让父组件刷新项目。
- ShotCard 新 prop：`onOpenGenerateImage: (shotId: number, slot: 'first_frame' | 'tail_frame') => void`；原 `onGenerateFirstFrame`/`onGenerateTailFrame` props 删除（`grep -n onGenerateFirstFrame frontend-vite/src` 找全调用点一并改）。

- [ ] **Step 1: 实现 GenerateImageDialog（对照 `design/exports/04-generate-image-dialog.png`）**

```tsx
// frontend-vite/src/components/GenerateImageDialog.tsx
// 统一图片生成弹窗：槽位切换 / 自定义提示词(可选,缺省自动) / 参考图勾选+临时上传 / 候选画廊
import { useMemo, useRef, useState } from 'react'
import { Loader2, Plus, Sparkles, Check } from 'lucide-react'
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { api } from '@/lib/api'
import type { ImageCandidate, ProjectDetail, Shot } from '@/lib/types'

interface Props {
  project: ProjectDetail
  shot: Shot
  slot: 'first_frame' | 'tail_frame'
  open: boolean
  onOpenChange: (open: boolean) => void
  onChanged: () => void
}

const SLOT_LABEL = { first_frame: '首帧', tail_frame: '尾帧' } as const

export function GenerateImageDialog({ project, shot, slot: initialSlot, open, onOpenChange, onChanged }: Props) {
  const [slot, setSlot] = useState<'first_frame' | 'tail_frame'>(initialSlot)
  const [prompt, setPrompt] = useState('')
  // 默认勾选所有 character 参考图（与后端缺省一致）
  const [checkedRefIds, setCheckedRefIds] = useState<Set<string>>(
    () => new Set(project.reference_images.filter(r => r.kind === 'character').map(r => r.id)),
  )
  const [tempFiles, setTempFiles] = useState<File[]>([])
  const [submitting, setSubmitting] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  const candidates = useMemo(
    () => (shot.image_candidates ?? []).filter(c => c.slot === slot),
    [shot.image_candidates, slot],
  )

  const autoHint = slot === 'tail_frame'
    ? '提示词留空时自动推理：分镜动作提示词 + 首帧 → 推导尾帧（两步 CoT）'
    : '提示词留空时自动推理：画面描述 + 本镜尾帧（如有）→ 反推首帧（两步 CoT）'

  const toggleRef = (id: string) => {
    setCheckedRefIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }

  const handleGenerate = async () => {
    setSubmitting(true)
    try {
      await api.createImageCandidate(project.id, shot.shot_id, {
        slot,
        customPrompt: prompt || undefined,
        refImageIds: [...checkedRefIds],
        files: tempFiles.length ? tempFiles : undefined,
      })
      setTempFiles([])
      onChanged()
    } finally {
      setSubmitting(false)
    }
  }

  const handleAdopt = async (c: ImageCandidate) => {
    await api.adoptImageCandidate(project.id, shot.shot_id, c.id)
    onChanged()
  }

  const handleDelete = async (c: ImageCandidate) => {
    await api.deleteImageCandidate(project.id, shot.shot_id, c.id)
    onChanged()
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>生成图片 — Shot #{shot.shot_id}</DialogTitle>
        </DialogHeader>

        {/* 目标槽位 */}
        <div className="space-y-2">
          <div className="text-sm font-medium text-zinc-700">
            目标槽位 <span className="text-xs font-normal text-zinc-400">（采纳的候选写入该槽位）</span>
          </div>
          <div className="inline-flex rounded-lg bg-zinc-100 p-0.5">
            {(['first_frame', 'tail_frame'] as const).map(s => (
              <button
                key={s}
                onClick={() => setSlot(s)}
                className={`rounded-md px-3.5 py-1.5 text-sm ${
                  slot === s ? 'bg-white font-semibold text-blue-600 shadow-sm' : 'text-zinc-500'
                }`}
              >
                {SLOT_LABEL[s]}
              </button>
            ))}
          </div>
        </div>

        {/* 自动推理提示 */}
        <div className="flex items-center gap-2 rounded-md bg-blue-50 px-3 py-2 text-xs text-blue-700">
          <Sparkles className="h-3.5 w-3.5 shrink-0" />
          {autoHint}
        </div>

        {/* 自定义提示词 */}
        <div className="space-y-2">
          <div className="text-sm font-medium text-zinc-700">
            自定义提示词 <span className="text-xs font-normal text-zinc-400">（可选 · 填写后覆盖自动推理）</span>
          </div>
          <Textarea
            value={prompt}
            onChange={e => setPrompt(e.target.value)}
            placeholder="例如：少女转身面向大海，手中饮品举至胸前，广角逆光，保持人物身份与服装不变…"
            rows={3}
          />
        </div>

        {/* 参考图勾选 + 临时上传 */}
        <div className="space-y-2">
          <div className="text-sm font-medium text-zinc-700">
            参考图 <span className="text-xs font-normal text-zinc-400">（默认自动带「角色」参考；临时上传仅本次生效）</span>
          </div>
          <div className="flex flex-wrap gap-2.5">
            {project.reference_images.map(r => (
              <button
                key={r.id}
                onClick={() => toggleRef(r.id)}
                className={`relative h-[72px] w-[72px] overflow-hidden rounded-md border-2 ${
                  checkedRefIds.has(r.id) ? 'border-blue-600' : 'border-zinc-200'
                }`}
                title={`${r.kind} 参考图`}
              >
                <img src={r.url} className="h-full w-full object-cover" />
                {checkedRefIds.has(r.id) && (
                  <span className="absolute left-1 top-1 rounded bg-blue-600 p-0.5">
                    <Check className="h-3 w-3 text-white" />
                  </span>
                )}
              </button>
            ))}
            <button
              onClick={() => fileInput.current?.click()}
              className="flex h-[72px] w-[72px] flex-col items-center justify-center gap-1 rounded-md border border-dashed border-zinc-300 text-zinc-400"
            >
              <Plus className="h-4 w-4" />
              <span className="text-[11px]">临时上传{tempFiles.length ? ` (${tempFiles.length})` : ''}</span>
            </button>
            <input
              ref={fileInput}
              type="file"
              accept="image/*"
              multiple
              className="hidden"
              onChange={e => setTempFiles([...(e.target.files ?? [])])}
            />
          </div>
        </div>

        {/* 候选画廊 */}
        <div className="space-y-2 border-t border-zinc-100 pt-3">
          <div className="flex items-center justify-between text-sm">
            <span className="font-medium text-zinc-700">
              候选画廊 · {SLOT_LABEL[slot]}
              <span className="ml-1 text-xs font-normal text-zinc-400">（每次生成 1 张，累积可对比）</span>
            </span>
            <span className="text-xs text-zinc-400">{candidates.length} 张</span>
          </div>
          <div className="flex flex-wrap gap-3">
            {candidates.map(c => (
              <div key={c.id} className="flex flex-col items-center gap-1.5">
                {c.status === 'generating' ? (
                  <div className="flex h-40 w-24 flex-col items-center justify-center gap-1.5 rounded-md border border-zinc-200 bg-zinc-50">
                    <Loader2 className="h-5 w-5 animate-spin text-blue-600" />
                    <span className="text-[11px] text-zinc-500">生成中…</span>
                  </div>
                ) : c.status === 'failed' ? (
                  <div
                    className="flex h-40 w-24 items-center justify-center rounded-md border border-red-200 bg-red-50 p-1 text-center text-[11px] text-red-600"
                    title={c.error ?? ''}
                  >
                    生成失败
                  </div>
                ) : (
                  <div className={`relative h-40 w-24 overflow-hidden rounded-md border-2 ${
                    c.adopted_at ? 'border-blue-600' : 'border-zinc-300'
                  }`}>
                    <img src={c.file_path ?? ''} className="h-full w-full object-cover" />
                    {c.adopted_at && (
                      <span className="absolute left-1 top-1 flex items-center gap-0.5 rounded bg-blue-600 px-1.5 py-0.5 text-[10px] font-semibold text-white">
                        <Check className="h-2.5 w-2.5" /> 已采纳
                      </span>
                    )}
                  </div>
                )}
                <div className="flex gap-2 text-xs">
                  {c.status === 'done' && !c.adopted_at && (
                    <button className="font-semibold text-blue-600" onClick={() => handleAdopt(c)}>采纳</button>
                  )}
                  {c.status !== 'generating' && (
                    <button className="text-red-600" onClick={() => handleDelete(c)}>删除</button>
                  )}
                </div>
              </div>
            ))}
            {candidates.length === 0 && (
              <div className="text-xs text-zinc-400">暂无候选，点「生成」创建第一张</div>
            )}
          </div>
        </div>

        {/* 底部 */}
        <div className="flex items-center justify-between pt-1">
          <span className="text-xs text-zinc-400">生成走异步队列，可关闭弹窗稍后回来采纳</span>
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => onOpenChange(false)}>关闭</Button>
            <Button onClick={handleGenerate} disabled={submitting}>
              {submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
              生成 1 张候选
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
```

> 组件用现有 `@/components/ui/dialog|button|textarea`（TrimDialog 已在用）；`ReferenceImage` 前端类型的图片 url 字段名以 `types.ts` 现有定义为准（`grep -n "interface ReferenceImage" frontend-vite/src/lib/types.ts`，若字段是 `url`/`storage_path` 按实际改 `r.url`）。

- [ ] **Step 2: ShotCard 菜单接线**

`ShotCard.tsx`：props 里删掉 `onGenerateFirstFrame`/`onGenerateTailFrame`，加 `onOpenGenerateImage: (shotId: number, slot: 'first_frame' | 'tail_frame') => void`；:922-945 两个菜单项**保留原有的条件展开包装**（`...(cond ? [{...}] : [])` 结构不动），仅把 item 对象替换为：

```tsx
  { icon: Sparkles, label: '生成首帧…', onClick: () => onOpenGenerateImage(shot.shot_id, 'first_frame') }
```

```tsx
  { icon: Sparkles, label: '生成尾帧…', onClick: () => onOpenGenerateImage(shot.shot_id, 'tail_frame') }
```

（尾帧项去掉 `disabled: !shot.motion_prompt` —— worker 会走 director 兜底。）

- [ ] **Step 3: ShotsPage 状态与渲染**

```tsx
const [genDialog, setGenDialog] = useState<{ shotId: number; slot: 'first_frame' | 'tail_frame' } | null>(null)
```

ShotCard 传 `onOpenGenerateImage={(shotId, slot) => setGenDialog({ shotId, slot })}`；页面底部渲染：

```tsx
{genDialog && project && (() => {
  const shot = project.shots.find(s => s.shot_id === genDialog.shotId)
  return shot ? (
    <GenerateImageDialog
      project={project}
      shot={shot}
      slot={genDialog.slot}
      open
      onOpenChange={(o) => !o && setGenDialog(null)}
      onChanged={refetchProject}
    />
  ) : null
})()}
```

（`refetchProject` = ShotsPage 现有的项目刷新函数名，按实际名称接。）删除原 `handleGenerateTailFrame`/`handleGenerateFirstFrame` handlers 及对 `api.generateTailFrame`/`api.generateFirstFrame` 的调用（api.ts 里两个方法保留作兼容，不再被前端调用）。

- [ ] **Step 4: 验证**

Run: `cd frontend-vite && npx tsc -b`
Expected: 无错误。
手动冒烟（可选，需本地栈）：`podman compose -f deploy/docker-compose.dev.yml up -d` 后打开 `http://localhost:4000`，关键帧下拉出现「生成首帧…/生成尾帧…」，弹窗各区块渲染正常（**不要点生成**——真实计费）。

- [ ] **Step 5: Commit**

```bash
git add frontend-vite/src
git commit -m "feat(frontend): 统一图片生成弹窗 + 关键帧下拉生成入口"
```

---

### Task 13: 前端 — CC 候选采纳条

**Files:**
- Create: `frontend-vite/src/components/CcCandidateStrip.tsx`
- Modify: `frontend-vite/src/components/ShotCard.tsx`（尾帧缩略图区下方渲染）
- Test: `npx tsc -b`

**Interfaces:**
- Produces: `<CcCandidateStrip shot={Shot} currentLastFrame={string|null} onAdopt={(cid) => void} onDelete={(cid) => void} onRecalibrate={() => void} />` — 仅当存在未采纳的 cc 候选（status done/generating/failed）时渲染。
- ShotCard 新 props：`onAdoptCandidate: (shotId: number, candidateId: string) => void`、`onDeleteCandidate: (shotId: number, candidateId: string) => void`；再校准复用现有校准人物 handler（`grep -n "校准人物" frontend-vite/src/components/ShotCard.tsx` 找到现有 onClick 所调的 prop 名并复用）。

- [ ] **Step 1: 实现（对照 `design/exports/06-cc-candidates-strip.png`）**

```tsx
// frontend-vite/src/components/CcCandidateStrip.tsx
// CC 人物校准候选条：当前尾帧 → 候选对比 → 采纳/删除/再校准
import { ArrowRight, Loader2, RefreshCw, UserCheck } from 'lucide-react'
import type { Shot } from '@/lib/types'

interface Props {
  shot: Shot
  currentLastFrame: string | null
  onAdopt: (candidateId: string) => void
  onDelete: (candidateId: string) => void
  onRecalibrate: () => void
}

export function CcCandidateStrip({ shot, currentLastFrame, onAdopt, onDelete, onRecalibrate }: Props) {
  const ccCands = (shot.image_candidates ?? []).filter(c => c.slot === 'cc' && !c.adopted_at)
  if (ccCands.length === 0) return null
  const pending = ccCands.filter(c => c.status === 'done').length

  return (
    <div className="space-y-2.5 rounded-lg border border-zinc-200 bg-white p-3">
      <div className="flex items-center justify-between">
        <span className="flex items-center gap-1.5 text-[13px] font-semibold text-zinc-700">
          <UserCheck className="h-3.5 w-3.5 text-zinc-600" /> 人物校准候选
        </span>
        {pending > 0 && (
          <span className="rounded-md bg-yellow-100 px-2 py-0.5 text-xs font-medium text-yellow-700">
            {pending} 张待采纳
          </span>
        )}
      </div>
      <div className="flex items-center gap-2.5">
        <div className="flex flex-col items-center gap-1">
          <div className="h-[135px] w-[76px] overflow-hidden rounded-md bg-zinc-300">
            {currentLastFrame && <img src={currentLastFrame} className="h-full w-full object-cover" />}
          </div>
          <span className="text-[11px] text-zinc-500">当前尾帧</span>
        </div>
        <ArrowRight className="h-4 w-4 shrink-0 text-zinc-400" />
        {ccCands.map(c => (
          <div key={c.id} className="flex flex-col items-center gap-1">
            {c.status === 'generating' ? (
              <div className="flex h-[135px] w-[76px] items-center justify-center rounded-md border border-zinc-200 bg-zinc-50">
                <Loader2 className="h-4 w-4 animate-spin text-blue-600" />
              </div>
            ) : c.status === 'failed' ? (
              <div className="flex h-[135px] w-[76px] items-center justify-center rounded-md border border-red-200 bg-red-50 p-1 text-center text-[11px] text-red-600" title={c.error ?? ''}>
                失败
              </div>
            ) : (
              <div className="h-[135px] w-[76px] overflow-hidden rounded-md border-2 border-blue-500">
                <img src={c.file_path ?? ''} className="h-full w-full object-cover" />
              </div>
            )}
            <div className="flex gap-2 text-xs">
              {c.status === 'done' && (
                <button className="font-semibold text-blue-600" onClick={() => onAdopt(c.id)}>采纳</button>
              )}
              {c.status !== 'generating' && (
                <button className="text-red-600" onClick={() => onDelete(c.id)}>删除</button>
              )}
            </div>
          </div>
        ))}
        <button
          onClick={onRecalibrate}
          className="flex h-[135px] w-[76px] flex-col items-center justify-center gap-1 rounded-md border border-dashed border-zinc-300 text-zinc-400"
        >
          <RefreshCw className="h-4 w-4" />
          <span className="text-[11px]">再校准一次</span>
        </button>
      </div>
      <p className="text-xs text-zinc-400">采纳后替换本镜尾帧（自动保留 pre-CC 备份，可随时还原）</p>
    </div>
  )
}
```

- [ ] **Step 2: 嵌入 ShotCard 并接线**

在 ShotCard 尾帧缩略图区（KeyframeSlot 渲染处 :913-945 之后）渲染：

```tsx
<CcCandidateStrip
  shot={shot}
  currentLastFrame={shot.last_frame_path}
  onAdopt={(cid) => onAdoptCandidate(shot.shot_id, cid)}
  onDelete={(cid) => onDeleteCandidate(shot.shot_id, cid)}
  onRecalibrate={/* 现有 校准人物 的同一 handler */}
/>
```

ShotsPage 里两个新 handler 调 `api.adoptImageCandidate`/`api.deleteImageCandidate` 后刷新项目。

- [ ] **Step 3: 验证 + Commit**

Run: `cd frontend-vite && npx tsc -b`
Expected: 无错误

```bash
git add frontend-vite/src
git commit -m "feat(frontend): CC 校准候选采纳条"
```

---

### Task 14: Playwright e2e — 候选采纳真实链路

**Files:**
- Create: `tests/helpers/seed_candidate.py`
- Modify: `tests/helpers/api.ts`（`seedImageCandidate` 包装）
- Create: `tests/e2e/image-candidates.spec.ts`

**Interfaces:**
- 遵守 CLAUDE.md e2e 规则：只 stub AI 触发端点 `POST **/image-candidates`（返回真实 202 形状）；候选行 + 真实图片文件直插真实 DB/存储；**adopt 走真实后端**，断言真实 `GET /api/projects/{id}` 的槽位路径变化。
- Produces: `seedImageCandidate(projectId: string, shotId: number, slot: string): string`（返回 candidate_id）。

- [ ] **Step 1: seed 脚本**

```python
# tests/helpers/seed_candidate.py
#!/usr/bin/env python3
"""Insert a DONE ImageCandidate with a real image file. Prints candidate_id.
Usage: python seed_candidate.py '{"project_id": "...", "shot_id": 1, "slot": "tail_frame"}'
"""
import asyncio
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'backend'))

from app.db import AsyncSession, init_db
from app.models.project import ImageCandidate, Shot
from app.services.storage import shot_candidates_dir, ts_uuid_name
from sqlalchemy import select

FIXTURE = Path(__file__).parent.parent / 'fixtures' / 'test-character.jpg'


async def main(args: dict) -> str:
    await init_db()
    pid, shot_seq, slot = args["project_id"], int(args["shot_id"]), args["slot"]

    dest_dir = shot_candidates_dir(pid, shot_seq)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / ts_uuid_name(".jpg")
    shutil.copy2(FIXTURE, dest)  # 真实已生成资产复用（不调模型）

    async with AsyncSession() as session:
        shot = (await session.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == shot_seq)
        )).scalar_one()
        cand = ImageCandidate(
            project_id=pid, shot_pk=shot.id, shot_id=shot_seq, slot=slot,
            status="done", file_path=str(dest), prompt_source="auto",
        )
        session.add(cand)
        await session.commit()
        await session.refresh(cand)
        return cand.id


if __name__ == "__main__":
    print(asyncio.run(main(json.loads(sys.argv[1]))))
```

`tests/helpers/api.ts` 加（完全镜像 `seedProjectState`（:56）的 env-file + execSync 调用方式，仅换脚本名与参数）：

```typescript
export function seedImageCandidate(projectId: string, shotId: number, slot: string): string {
  const scriptPath = path.resolve(__dirname, 'seed_candidate.py')
  const projectRoot = path.resolve(__dirname, '../..')
  const backendDir = path.join(projectRoot, 'backend')
  const devDb = path.join(projectRoot, 'backend', 'data', 'dev.db')
  const envFile = path.join(backendDir, '.env.test-seed')
  fs.writeFileSync(envFile, `DATABASE_URL=sqlite+aiosqlite:///${devDb}\n`)
  const argsJson = JSON.stringify({ project_id: projectId, shot_id: shotId, slot })
  const result = execSync(
    `uv run --env-file .env.test-seed --project . python ${scriptPath} '${argsJson}'`,
    { cwd: backendDir, encoding: 'utf8' }
  ).trim()
  return result.split('\n').pop()!
}
```

- [ ] **Step 2: spec**

```typescript
// tests/e2e/image-candidates.spec.ts
/**
 * 统一图片生成候选流。
 * - 生成触发（AI 计费点）：仅 stub POST image-candidates，断言弹窗发出的请求参数。
 * - 采纳链路：候选行+真实图片直插 DB（seedImageCandidate），adopt 走真实后端，
 *   断言真实 GET /api/projects/{id} 反映新的 target_last_frame_path。
 */
import { test, expect } from '@playwright/test'
import { seedProjectState, seedImageCandidate, deleteProject, getProject } from '../helpers/api'

let projectId: string

test.beforeAll(() => {
  projectId = seedProjectState('shot_review', { title: 'PW ImageCandidates' })
})

test.afterAll(async () => {
  await deleteProject(projectId)
})

test('生成弹窗从关键帧下拉打开并发出真实形状的创建请求', async ({ page }) => {
  let captured: Record<string, string> | null = null
  await page.route(`**/api/projects/${projectId}/shots/*/image-candidates`, async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    const buf = route.request().postDataBuffer()
    captured = Object.fromEntries(
      [...buf!.toString().matchAll(/name="([^"]+)"\r\n\r\n([^\r]*)/g)].map(m => [m[1], m[2]]),
    )
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({
        status: 'queued',
        candidate: {
          id: 'stub', shot_id: 1, slot: 'tail_frame', status: 'generating',
          file_path: null, prompt_source: 'custom', custom_prompt: '自定义提示词',
          error: null, created_at: new Date().toISOString(), adopted_at: null,
        },
      }),
    })
  })

  await page.goto(`/projects/${projectId}`)
  // 打开尾帧槽位的关键帧下拉 → 生成尾帧…
  await page.getByText('目标尾帧').first().click()
  await page.getByText('生成尾帧…').click()
  await expect(page.getByText('生成图片 — Shot #1')).toBeVisible()

  await page.getByPlaceholder(/自定义合成/).fill('自定义提示词')
  await page.getByRole('button', { name: /生成 1 张候选/ }).click()
  await expect.poll(() => captured).not.toBeNull()
  expect(captured!.slot).toBe('tail_frame')
  expect(captured!.custom_prompt).toBe('自定义提示词')
})

test('采纳候选走真实后端并写入尾帧槽位', async ({ page }) => {
  seedImageCandidate(projectId, 1, 'tail_frame')

  await page.goto(`/projects/${projectId}`)
  await page.getByText('目标尾帧').first().click()
  await page.getByText('生成尾帧…').click()
  await expect(page.getByText('候选画廊 · 尾帧')).toBeVisible()

  await page.getByRole('button', { name: '采纳' }).first().click()
  await expect(page.getByText('已采纳')).toBeVisible()

  // 真实后端状态：target_last_frame_path 已写入且 tf_status=done
  const proj = await getProject(projectId) as { shots: Array<Record<string, unknown>> }
  const shot1 = proj.shots.find(s => s.shot_id === 1)!
  expect(shot1.target_last_frame_path).toBeTruthy()
  expect(shot1.tf_status).toBe('done')
  const cands = shot1.image_candidates as Array<Record<string, unknown>>
  expect(cands.some(c => c.adopted_at)).toBe(true)
})
```

> 页面路径（`/projects/${projectId}`）与打开下拉的选择器（`目标尾帧` 文案 / KeyframeSlot 的 trigger）以现有 e2e spec 与 ShotCard 实际 DOM 为准（参考 `tests/e2e/subplan5-shot-review.spec.ts` 的导航方式），跑失败时用 `npx playwright test --debug` 校正 —— 但**断言目标**（captured form 字段、真实 getProject 的槽位字段）不得改成 mock 数据。

- [ ] **Step 3: 跑 e2e**

前置：本地栈已跑（`podman compose -f deploy/docker-compose.dev.yml up -d`，后端改动后 `podman restart video-maker-backend-dev video-maker-worker-dev`）。

Run（Playwright 配置所在目录，与现有 tests/e2e 跑法一致）: `npx playwright test tests/e2e/image-candidates.spec.ts`
Expected: 2 PASS

- [ ] **Step 4: Commit**

```bash
git add tests/helpers/seed_candidate.py tests/helpers/api.ts tests/e2e/image-candidates.spec.ts
git commit -m "test(e2e): 候选生成请求形状 + 真实采纳链路"
```

---

## 收尾核对（最后一个任务执行完后）

- [ ] `cd backend && uv run pytest tests/ -q` 全绿
- [ ] `cd frontend-vite && npx tsc -b` 无错误
- [ ] `grep -rn "tail_frame_generator\|first_frame_generator\|face_calibration_client\|run_tail_frame_pipeline\|run_first_frame_pipeline" backend/ frontend-vite/src/` 无残留
- [ ] 素材审计清单（spec「素材文件变更审计」小节）逐项过一遍
- [ ] `podman restart video-maker-backend-dev video-maker-worker-dev` 后 `curl -s localhost:8002/openapi.json | grep image-candidates` 能看到新路由
