# 台词与动作生成 MCP 服务 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a remote HTTP/SSE MCP service that lets an external LLM agent read shot context and author/edit character dialogue (`text`/台词) and motion (`motion_prompt`/动作) — including full-storyboard replacement — by bridging the existing video_maker backend HTTP API.

**Architecture:** A new `backend/mcp_server/` package runs as a separate compose service built from the existing backend image (same pattern as `worker`). It calls the backend over the internal compose network via `httpx` (the backend remains the single source of truth, preserving its state machine and events). The MCP itself makes **no LLM calls** — generation happens in the calling agent. A new backend endpoint `PUT /api/projects/{id}/storyboard` adds full-replace (create/delete) semantics the existing `PATCH` lacks.

**Tech Stack:** Python 3.12, FastAPI + async SQLAlchemy (existing backend), `fastmcp` (v2, HTTP transport + in-memory test client), `httpx`, `pytest`/`pytest-asyncio`, `uv`, podman compose.

## Global Constraints

- Python ≥ 3.12; manage packages via `backend/pyproject.toml`, run via `uv run --project backend ...`. Never call `python`/`pip` directly.
- Run backend tests with `uv run --project backend pytest ...` directly (not via podman).
- The MCP service makes **no** LLM/model calls — nothing to mock for billing; tests use real in-memory SQLite via the backend ASGI app.
- No hardcoded absolute paths — use `pathlib` relative to `__file__`, `settings`, or env vars.
- The new `PUT /storyboard` endpoint must obey CLAUDE.md "Shot 素材文件变更审计": deleting a shot must remove any leftover shot output directory.
- Backend HTTP calls must send header `X-User-Name` (backend `_require_user` returns 400 without it).
- Backend container/service name is `video-maker-backend-dev`, internal port `8002` → `BACKEND_BASE_URL=http://video-maker-backend-dev:8002`.
- Storyboard JSON shot shape = `ShotItem`: `{shot_id, text, shot_type, visual_description, shot_duration, align_with_previous, reference_image_hint?}` — **no `motion_prompt`**.
- `shot_type` ∈ {`Close-up`, `Medium Shot`, `Wide Shot`}; `shot_duration` ∈ {4, 6, 8}.

---

## File Structure

**Backend endpoint (Tasks 1–2)**
- Modify `backend/app/models/schemas.py` — add `StoryboardReplace` request model.
- Modify `backend/app/api/pipeline.py` — add `PUT /projects/{id}/storyboard` handler.
- Modify `backend/tests/integration/test_pipeline.py` — endpoint tests.

**MCP package (Tasks 3–8)** — all under `backend/mcp_server/`
- `__init__.py` — package marker.
- `config.py` — reads `BACKEND_BASE_URL`, `MCP_HOST`, `MCP_PORT` from env.
- `client.py` — `BackendClient`: async httpx wrapper over the backend API.
- `validation.py` — `word_count_report()` (reuses `screenwriter.WORD_COUNT_RULES`/`validate_word_count`).
- `context.py` — `shape_project()`, `shape_shot()`, `with_neighbors()` shaping helpers.
- `guidelines.py` — `AUTHORING_GUIDELINES` constant (distilled dialogue/motion rules).
- `server.py` — `create_server(backend) -> FastMCP` (registers all tools) + `main()` entrypoint.
- `tests/mcp/conftest.py` — fixtures: in-memory backend app + `BackendClient` wired via `ASGITransport`.
- `tests/mcp/test_client.py`, `tests/mcp/test_validation.py`, `tests/mcp/test_context.py`, `tests/mcp/test_read_tools.py`, `tests/mcp/test_write_tools.py`.

**Deploy/docs (Tasks 9–10)**
- Modify `backend/pyproject.toml` — add `fastmcp` dependency.
- Modify `deploy/docker-compose.dev.yml` — add `mcp` service.
- Modify `Makefile` — add `dev-mcp` target + include `mcp` note.
- Modify backend docs — document endpoint + MCP tools.

---

## Task 1: Backend `PUT /storyboard` full-replace endpoint

**Files:**
- Modify: `backend/app/models/schemas.py` (add `StoryboardReplace` after `StoryboardUpdate`, ~line 114)
- Modify: `backend/app/api/pipeline.py` (add handler after `patch_storyboard`, ~line 264; extend schema import ~line 22)
- Test: `backend/tests/integration/test_pipeline.py`

**Interfaces:**
- Consumes: existing `ShotItem` schema; fixtures `project_in_script_review` (status `script_review`, 3 pending shots, scene_overview set), `project_in_shot_review` (status `shot_review`), `db_session_factory`, `client`, `HEADERS` from `tests/integration/conftest.py`.
- Produces: `PUT /api/projects/{project_id}/storyboard` accepting `{scene_overview: str, shots: ShotItem[]}`, returning `ProjectResponse`; new schema `StoryboardReplace`.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/integration/test_pipeline.py`:

```python
# ── PUT /projects/{id}/storyboard (full replace) ──────────────────────────────

async def test_put_storyboard_upsert_and_add(client, db_session_factory, project_in_script_review):
    pid = project_in_script_review  # has shots 1,2,3
    r = await client.put(
        f"/api/projects/{pid}/storyboard",
        json={
            "scene_overview": "new overview",
            "shots": [
                {"shot_id": 1, "text": "edited one", "shot_type": "Close-up",
                 "visual_description": "v1", "shot_duration": 4, "align_with_previous": False},
                {"shot_id": 4, "text": "brand new", "shot_type": "Wide Shot",
                 "visual_description": "v4", "shot_duration": 8, "align_with_previous": True},
            ],
        },
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    from app.models.project import Shot
    from sqlalchemy import select
    async with db_session_factory() as s:
        rows = (await s.execute(select(Shot).where(Shot.project_id == pid))).scalars().all()
    by_id = {row.shot_id: row for row in rows}
    assert set(by_id) == {1, 4}            # shots 2,3 deleted; 4 created
    assert by_id[1].text == "edited one"
    assert by_id[1].shot_type == "Close-up"
    assert by_id[4].text == "brand new"


async def test_put_storyboard_rewrites_json(client, db_session_factory, project_in_script_review, tmp_path):
    pid = project_in_script_review
    r = await client.put(
        f"/api/projects/{pid}/storyboard",
        json={"scene_overview": "ov", "shots": [
            {"shot_id": 1, "text": "only", "shot_type": "Medium Shot",
             "visual_description": "v", "shot_duration": 6, "align_with_previous": False},
        ]},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    import json
    from app.services.storage import storyboard_path
    data = json.loads(storyboard_path(pid).read_text(encoding="utf-8"))
    assert data["scene_overview"] == "ov"
    assert [s["shot_id"] for s in data["shots"]] == [1]
    assert data["shots"][0]["text"] == "only"


async def test_put_storyboard_wrong_status(client, project_in_shot_review):
    pid = project_in_shot_review
    r = await client.put(
        f"/api/projects/{pid}/storyboard",
        json={"scene_overview": "x", "shots": [
            {"shot_id": 1, "text": "t", "shot_type": "Medium Shot",
             "visual_description": "v", "shot_duration": 6, "align_with_previous": False}]},
        headers=HEADERS,
    )
    assert r.status_code == 409


async def test_put_storyboard_not_found(client):
    r = await client.put(
        "/api/projects/nonexistent/storyboard",
        json={"scene_overview": "x", "shots": [
            {"shot_id": 1, "text": "t", "shot_type": "Medium Shot",
             "visual_description": "v", "shot_duration": 6, "align_with_previous": False}]},
        headers=HEADERS,
    )
    assert r.status_code == 404


async def test_put_storyboard_duplicate_shot_id(client, project_in_script_review):
    pid = project_in_script_review
    r = await client.put(
        f"/api/projects/{pid}/storyboard",
        json={"scene_overview": "x", "shots": [
            {"shot_id": 1, "text": "a", "shot_type": "Medium Shot",
             "visual_description": "v", "shot_duration": 6, "align_with_previous": False},
            {"shot_id": 1, "text": "b", "shot_type": "Medium Shot",
             "visual_description": "v", "shot_duration": 6, "align_with_previous": False}]},
        headers=HEADERS,
    )
    assert r.status_code == 422


async def test_put_storyboard_empty_shots(client, project_in_script_review):
    pid = project_in_script_review
    r = await client.put(
        f"/api/projects/{pid}/storyboard",
        json={"scene_overview": "x", "shots": []},
        headers=HEADERS,
    )
    assert r.status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project backend pytest backend/tests/integration/test_pipeline.py -k put_storyboard -v`
Expected: FAIL — `405 Method Not Allowed` (route missing) / status assertions fail.

- [ ] **Step 3: Add the `StoryboardReplace` schema**

In `backend/app/models/schemas.py`, immediately after the `StoryboardUpdate` class (~line 114), add:

```python
class StoryboardReplace(BaseModel):
    """Full-replace storyboard: both fields required (vs StoryboardUpdate's optionals)."""
    scene_overview: str
    shots: List[ShotItem] = Field(..., min_length=1)

    @field_validator("shots")
    @classmethod
    def _unique_shot_ids(cls, v: List[ShotItem]) -> List[ShotItem]:
        ids = [s.shot_id for s in v]
        if len(ids) != len(set(ids)):
            raise ValueError("shot_id values must be unique")
        return v
```

Ensure `field_validator` is imported at the top of `schemas.py` (add to the existing pydantic import if absent):

```python
from pydantic import BaseModel, Field, field_validator
```

- [ ] **Step 4: Add the endpoint handler**

In `backend/app/api/pipeline.py`, add `StoryboardReplace` to the `app.models.schemas` import block (~line 22), then add this handler right after `patch_storyboard` (after line 263):

```python
@router.put("/projects/{project_id}/storyboard", response_model=ProjectResponse)
async def put_storyboard(
    project_id: str,
    body: StoryboardReplace,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """Full-replace storyboard: upsert shots by shot_id, delete missing, rewrite storyboard.json.

    Only allowed in SCRIPT_REVIEW (pre-render): no generated material files at stake.
    """
    project = await _get_project_or_404(project_id, session)

    if project.status != ProjectStatus.SCRIPT_REVIEW.value:
        raise HTTPException(
            status_code=409,
            detail="Project must be in script_review status to replace storyboard",
        )

    result = await session.execute(select(Shot).where(Shot.project_id == project_id))
    existing = {s.shot_id: s for s in result.scalars().all()}
    payload_ids = {item.shot_id for item in body.shots}

    # Delete shots absent from the payload (defensive file cleanup handled in Task 2).
    for shot_id, shot in existing.items():
        if shot_id not in payload_ids:
            await session.delete(shot)

    # Upsert shots present in the payload.
    for item in body.shots:
        shot = existing.get(item.shot_id)
        if shot is None:
            shot = Shot(project_id=project_id, shot_id=item.shot_id)
            session.add(shot)
        shot.text = item.text
        shot.shot_type = item.shot_type
        shot.visual_description = item.visual_description
        shot.shot_duration = item.shot_duration
        shot.align_with_previous = item.align_with_previous
        shot.reference_image_hint = item.reference_image_hint

    project.scene_overview = body.scene_overview

    # Rewrite storyboard.json to match (DB is source of truth).
    sb_path = storyboard_path(project_id)
    sb_path.parent.mkdir(parents=True, exist_ok=True)
    sb_path.write_text(
        json.dumps(
            {
                "scene_overview": body.scene_overview,
                "shots": [item.model_dump() for item in body.shots],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    project.storyboard_path = str(sb_path)
    project.updated_at = datetime.utcnow()
    session.add(project)
    await session.commit()
    await session.refresh(project)

    from app.models.schemas import Storyboard
    storyboard = None
    if project.storyboard_path:
        try:
            sb_data = json.loads(Path(project.storyboard_path).read_text())
            storyboard = Storyboard(**sb_data)
        except Exception:
            pass

    return ProjectResponse(
        id=project.id,
        title=project.title,
        theme_text=project.theme_text,
        creator_name=project.creator_name,
        status=project.status,
        scene_overview=project.scene_overview,
        storyboard_path=project.storyboard_path,
        final_video_path=project.final_video_path,
        error_message=project.error_message,
        created_at=project.created_at,
        updated_at=project.updated_at,
        reference_images=[],
        shots=[],
        storyboard=storyboard,
    )
```

`storyboard_path` is already imported in `pipeline.py` (used by other handlers); if not, add `from app.services.storage import storyboard_path`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --project backend pytest backend/tests/integration/test_pipeline.py -k put_storyboard -v`
Expected: all 6 PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/schemas.py backend/app/api/pipeline.py backend/tests/integration/test_pipeline.py
git commit -m "feat(api): add PUT /storyboard full-replace endpoint"
```

---

## Task 2: Defensive material-file cleanup on shot deletion

**Files:**
- Modify: `backend/app/api/pipeline.py` (the delete branch of `put_storyboard`)
- Test: `backend/tests/integration/test_pipeline.py`

**Interfaces:**
- Consumes: `shot_dir(project_id, shot_id)` from `app.services.storage`; `settings.storage_root` (overridden to `tmp_path` by the `client` fixture).
- Produces: deleting a shot removes its `shot_dir` if present.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/integration/test_pipeline.py`:

```python
async def test_put_storyboard_deletes_shot_output_dir(client, db_session_factory, project_in_script_review):
    pid = project_in_script_review  # shots 1,2,3
    from app.services.storage import shot_dir
    leftover = shot_dir(pid, 3)
    leftover.mkdir(parents=True, exist_ok=True)
    (leftover / "output.mp4").write_bytes(b"stale")
    assert leftover.exists()

    r = await client.put(
        f"/api/projects/{pid}/storyboard",
        json={"scene_overview": "ov", "shots": [
            {"shot_id": 1, "text": "keep", "shot_type": "Medium Shot",
             "visual_description": "v", "shot_duration": 6, "align_with_previous": False}]},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    assert not leftover.exists()  # shot 3 dir removed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project backend pytest backend/tests/integration/test_pipeline.py::test_put_storyboard_deletes_shot_output_dir -v`
Expected: FAIL — `leftover.exists()` still True.

- [ ] **Step 3: Add cleanup to the delete branch**

In `put_storyboard`, replace the delete loop body so it also removes leftover dirs. Add `from app.services.storage import shot_dir` to the storage import in `pipeline.py`, then:

```python
    # Delete shots absent from the payload + remove any leftover output dir (CLAUDE.md audit).
    for shot_id, shot in existing.items():
        if shot_id not in payload_ids:
            await session.delete(shot)
            s_dir = shot_dir(project_id, shot_id)
            if s_dir.exists():
                shutil.rmtree(s_dir, ignore_errors=True)
```

`shutil` is already imported at the top of `pipeline.py` (line 5).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --project backend pytest backend/tests/integration/test_pipeline.py -k put_storyboard -v`
Expected: all 7 PASS (incl. the new one; earlier 6 still green).

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/pipeline.py backend/tests/integration/test_pipeline.py
git commit -m "feat(api): clean up leftover shot dir on storyboard replace delete"
```

---

## Task 3: MCP package scaffold + config + `fastmcp` dependency

**Files:**
- Modify: `backend/pyproject.toml` (add `fastmcp`)
- Create: `backend/mcp_server/__init__.py`
- Create: `backend/mcp_server/config.py`
- Create: `backend/tests/mcp/__init__.py`
- Test: `backend/tests/mcp/test_config.py`

**Interfaces:**
- Produces: `mcp_server.config.Settings` with attrs `backend_base_url: str`, `mcp_host: str`, `mcp_port: int`; module-level `settings = Settings()`.

- [ ] **Step 1: Add the dependency**

In `backend/pyproject.toml`, add `"fastmcp>=2.0"` to the `[project].dependencies` list. Then:

Run: `uv sync --project backend`
Expected: resolves and installs `fastmcp`.

- [ ] **Step 2: Write the failing test**

Create `backend/tests/mcp/__init__.py` (empty), then `backend/tests/mcp/test_config.py`:

```python
def test_config_defaults(monkeypatch):
    monkeypatch.delenv("BACKEND_BASE_URL", raising=False)
    monkeypatch.delenv("MCP_PORT", raising=False)
    from importlib import reload
    import mcp_server.config as cfg
    reload(cfg)
    assert cfg.Settings().backend_base_url == "http://localhost:8002"
    assert cfg.Settings().mcp_port == 8765


def test_config_env_override(monkeypatch):
    monkeypatch.setenv("BACKEND_BASE_URL", "http://video-maker-backend-dev:8002")
    monkeypatch.setenv("MCP_PORT", "9000")
    from importlib import reload
    import mcp_server.config as cfg
    reload(cfg)
    s = cfg.Settings()
    assert s.backend_base_url == "http://video-maker-backend-dev:8002"
    assert s.mcp_port == 9000
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run --project backend pytest backend/tests/mcp/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_server'`.

- [ ] **Step 4: Create the package + config**

Create `backend/mcp_server/__init__.py`:

```python
"""MCP server bridging the video_maker backend for dialogue/action authoring."""
```

Create `backend/mcp_server/config.py`:

```python
import os


class Settings:
    def __init__(self) -> None:
        self.backend_base_url: str = os.getenv("BACKEND_BASE_URL", "http://localhost:8002")
        self.mcp_host: str = os.getenv("MCP_HOST", "0.0.0.0")
        self.mcp_port: int = int(os.getenv("MCP_PORT", "8765"))


settings = Settings()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run --project backend pytest backend/tests/mcp/test_config.py -v`
Expected: both PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/pyproject.toml backend/uv.lock backend/mcp_server/__init__.py backend/mcp_server/config.py backend/tests/mcp/__init__.py backend/tests/mcp/test_config.py
git commit -m "feat(mcp): scaffold mcp_server package + config + fastmcp dep"
```

---

## Task 4: `BackendClient` HTTP wrapper

**Files:**
- Create: `backend/mcp_server/client.py`
- Test: `backend/tests/mcp/conftest.py`, `backend/tests/mcp/test_client.py`

**Interfaces:**
- Consumes: an `httpx.AsyncClient` (injectable for tests via `ASGITransport`); the backend ASGI `app` + in-memory DB setup mirrored from `tests/integration/conftest.py`.
- Produces: `class BackendClient` with async methods:
  - `list_projects() -> list[dict]`
  - `get_project(project_id: str) -> dict`
  - `patch_shot(project_id: str, shot_id: int, body: dict) -> dict`
  - `replace_storyboard(project_id: str, scene_overview: str, shots: list[dict]) -> dict`
  - All send header `X-User-Name: mcp-agent`. Non-2xx raises `BackendError(status_code, detail)`.

- [ ] **Step 1: Write the MCP test conftest**

Create `backend/tests/mcp/conftest.py` (reuses the integration DB-wiring approach + exposes a `BackendClient`):

```python
"""Fixtures for MCP server tests: real backend ASGI app over in-memory SQLite."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.project import Base, Project, Shot, ReferenceImage

USER = "mcp-agent"


@pytest.fixture
async def db_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session_factory(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def http_client(db_engine, db_session_factory, monkeypatch, tmp_path):
    from app.main import app, get_redis
    from app.db import get_session
    import app.db as db_module
    import app.api.stream as stream_module
    import app.api.pipeline as pipeline_module
    from app.config import settings

    monkeypatch.setattr(db_module, "AsyncSession", db_session_factory)
    monkeypatch.setattr(stream_module, "session_factory", db_session_factory)
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))

    arq = MagicMock()
    arq.enqueue_job = AsyncMock(return_value=None)

    async def _fake_get_arq(_redis):
        return arq
    monkeypatch.setattr(pipeline_module, "_get_arq_redis", _fake_get_arq)

    async def override_session():
        async with db_session_factory() as s:
            yield s

    async def override_redis():
        return AsyncMock()

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_redis] = override_redis

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def backend(http_client):
    from mcp_server.client import BackendClient
    return BackendClient(base_url="http://test", client=http_client)


# ── DB seed helpers ──────────────────────────────────────────────────────────

async def seed_project(sf, status="script_review", scene_overview="overview", shots=3):
    async with sf() as s:
        p = Project(title="P", theme_text="theme", creator_name=USER,
                    status=status, scene_overview=scene_overview)
        s.add(p)
        await s.commit()
        await s.refresh(p)
        pid = p.id
        for i in range(1, shots + 1):
            s.add(Shot(project_id=pid, shot_id=i, text=f"line {i}",
                       shot_type="Medium Shot", visual_description=f"v{i}",
                       shot_duration=6, status="pending", align_with_previous=(i > 1)))
        await s.commit()
        return pid
```

- [ ] **Step 2: Write the failing client test**

Create `backend/tests/mcp/test_client.py`:

```python
import pytest
from tests.mcp.conftest import seed_project


async def test_list_and_get_project(backend, db_session_factory):
    pid = await seed_project(db_session_factory)
    projects = await backend.list_projects()
    assert any(p["id"] == pid for p in projects)

    proj = await backend.get_project(pid)
    assert proj["id"] == pid
    assert len(proj["shots"]) == 3


async def test_patch_shot(backend, db_session_factory):
    pid = await seed_project(db_session_factory)
    out = await backend.patch_shot(pid, 1, {"text": "patched", "motion_prompt": "zoom in"})
    assert out["text"] == "patched"
    assert out["motion_prompt"] == "zoom in"


async def test_replace_storyboard(backend, db_session_factory):
    pid = await seed_project(db_session_factory)
    out = await backend.replace_storyboard(pid, "ov", [
        {"shot_id": 1, "text": "a", "shot_type": "Close-up",
         "visual_description": "v", "shot_duration": 4, "align_with_previous": False},
    ])
    assert out["scene_overview"] == "ov"


async def test_backend_error_on_404(backend):
    from mcp_server.client import BackendError
    with pytest.raises(BackendError) as ei:
        await backend.get_project("nope")
    assert ei.value.status_code == 404
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run --project backend pytest backend/tests/mcp/test_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_server.client'`.

- [ ] **Step 4: Implement the client**

Create `backend/mcp_server/client.py`:

```python
from typing import Any, Optional

import httpx

HEADERS = {"X-User-Name": "mcp-agent"}


class BackendError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"backend {status_code}: {detail}")


class BackendClient:
    """Async wrapper over the video_maker backend HTTP API."""

    def __init__(self, base_url: str, client: Optional[httpx.AsyncClient] = None):
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._owns_client = client is None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._base_url, timeout=30.0)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _request(self, method: str, path: str, json: Any = None) -> Any:
        client = await self._http()
        resp = await client.request(method, path, json=json, headers=HEADERS)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise BackendError(resp.status_code, str(detail))
        return resp.json()

    async def list_projects(self) -> list[dict]:
        data = await self._request("GET", "/api/projects")
        # ProjectList shape: {"projects": [...]}; tolerate a bare list too.
        return data["projects"] if isinstance(data, dict) and "projects" in data else data

    async def get_project(self, project_id: str) -> dict:
        return await self._request("GET", f"/api/projects/{project_id}")

    async def patch_shot(self, project_id: str, shot_id: int, body: dict) -> dict:
        return await self._request(
            "PATCH", f"/api/projects/{project_id}/shots/{shot_id}", json=body
        )

    async def replace_storyboard(
        self, project_id: str, scene_overview: str, shots: list[dict]
    ) -> dict:
        return await self._request(
            "PUT",
            f"/api/projects/{project_id}/storyboard",
            json={"scene_overview": scene_overview, "shots": shots},
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --project backend pytest backend/tests/mcp/test_client.py -v`
Expected: all 4 PASS. (If `list_projects` shape assertion fails, inspect `ProjectList` in `schemas.py` and adjust the unwrap — the code already tolerates both dict-with-`projects` and bare list.)

- [ ] **Step 6: Commit**

```bash
git add backend/mcp_server/client.py backend/tests/mcp/conftest.py backend/tests/mcp/test_client.py
git commit -m "feat(mcp): BackendClient httpx wrapper + test harness"
```

---

## Task 5: Validation + context-shaping helpers

**Files:**
- Create: `backend/mcp_server/validation.py`
- Create: `backend/mcp_server/context.py`
- Create: `backend/mcp_server/guidelines.py`
- Test: `backend/tests/mcp/test_validation.py`, `backend/tests/mcp/test_context.py`

**Interfaces:**
- Produces:
  - `validation.word_count_report(text: str, duration: int) -> dict` → `{"actual": int, "target_range": [int,int] | None, "within_range": bool}`
  - `context.shape_project(p: dict) -> dict` → `{id, theme, status, aspect_ratio, scene_overview, characters: [{filename, kind}], shot_count}`
  - `context.shape_shot(shot: dict) -> dict` → `{shot_id, order_index, shot_type, shot_duration, align_with_previous, text, motion_prompt, visual_description, word_count, word_count_target, has_video}`
  - `context.with_neighbors(shots: list[dict], shot_id: int) -> dict` → shaped shot + `{prev_text, next_text}`
  - `guidelines.AUTHORING_GUIDELINES: str`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/mcp/test_validation.py`:

```python
from mcp_server.validation import word_count_report


def test_within_range():
    r = word_count_report("one two three four five six seven eight", 4)  # 8 words
    assert r["actual"] == 8
    assert r["target_range"] == [8, 10]
    assert r["within_range"] is True


def test_out_of_range():
    r = word_count_report("too short", 8)  # 2 words vs 18-21
    assert r["within_range"] is False


def test_unknown_duration_passes():
    r = word_count_report("anything goes here", 5)
    assert r["target_range"] is None
    assert r["within_range"] is True
```

Create `backend/tests/mcp/test_context.py`:

```python
from mcp_server.context import shape_project, shape_shot, with_neighbors


def _shot(i, **kw):
    base = dict(id=i, shot_id=i, text=f"line {i}", shot_type="Medium Shot",
                visual_description=f"v{i}", shot_duration=6, status="pending",
                align_with_previous=(i > 1), motion_prompt=None, video_path=None)
    base.update(kw)
    return base


def test_shape_project_filters_characters():
    p = {"id": "p1", "theme_text": "t", "status": "script_review", "aspect_ratio": "16:9",
         "scene_overview": "ov",
         "reference_images": [{"filename": "c.jpg", "kind": "character"},
                              {"filename": "s.jpg", "kind": "scene"}],
         "shots": [_shot(1), _shot(2)]}
    out = shape_project(p)
    assert out == {"id": "p1", "theme": "t", "status": "script_review",
                   "aspect_ratio": "16:9", "scene_overview": "ov",
                   "characters": [{"filename": "c.jpg", "kind": "character"}],
                   "shot_count": 2}


def test_shape_shot_word_count_and_has_video():
    out = shape_shot(_shot(1, text="a b c d", shot_duration=4, video_path="/x/output.mp4"))
    assert out["word_count"] == 4
    assert out["word_count_target"] == [8, 10]
    assert out["has_video"] is True
    assert out["motion_prompt"] is None


def test_with_neighbors():
    shots = [_shot(1, text="first"), _shot(2, text="second"), _shot(3, text="third")]
    out = with_neighbors(shots, 2)
    assert out["shot_id"] == 2
    assert out["prev_text"] == "first"
    assert out["next_text"] == "third"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project backend pytest backend/tests/mcp/test_validation.py backend/tests/mcp/test_context.py -v`
Expected: FAIL — `ModuleNotFoundError` for `mcp_server.validation` / `mcp_server.context`.

- [ ] **Step 3: Implement `validation.py`**

Create `backend/mcp_server/validation.py`:

```python
from app.agents.screenwriter import WORD_COUNT_RULES, validate_word_count


def word_count_report(text: str, duration: int) -> dict:
    """Advisory word-count check; never blocks. Mirrors screenwriter rules."""
    actual = len((text or "").strip().split())
    target = WORD_COUNT_RULES.get(duration)
    return {
        "actual": actual,
        "target_range": list(target) if target else None,
        "within_range": validate_word_count(text or "", duration) if target else True,
    }
```

- [ ] **Step 4: Implement `context.py`**

Create `backend/mcp_server/context.py`:

```python
from mcp_server.validation import word_count_report


def shape_project(p: dict) -> dict:
    characters = [
        {"filename": r["filename"], "kind": r["kind"]}
        for r in p.get("reference_images", [])
        if r.get("kind") == "character"
    ]
    return {
        "id": p["id"],
        "theme": p.get("theme_text"),
        "status": p["status"],
        "aspect_ratio": p.get("aspect_ratio"),
        "scene_overview": p.get("scene_overview"),
        "characters": characters,
        "shot_count": len(p.get("shots", [])),
    }


def shape_shot(shot: dict) -> dict:
    wc = word_count_report(shot.get("text") or "", shot["shot_duration"])
    return {
        "shot_id": shot["shot_id"],
        "order_index": shot["shot_id"],  # shot_id is the ordering key
        "shot_type": shot["shot_type"],
        "shot_duration": shot["shot_duration"],
        "align_with_previous": shot["align_with_previous"],
        "text": shot.get("text"),
        "motion_prompt": shot.get("motion_prompt"),
        "visual_description": shot.get("visual_description"),
        "word_count": wc["actual"],
        "word_count_target": wc["target_range"],
        "has_video": bool(shot.get("video_path")),
    }


def with_neighbors(shots: list[dict], shot_id: int) -> dict:
    ordered = sorted(shots, key=lambda s: s["shot_id"])
    idx = next((i for i, s in enumerate(ordered) if s["shot_id"] == shot_id), None)
    if idx is None:
        raise KeyError(f"shot {shot_id} not found")
    shaped = shape_shot(ordered[idx])
    shaped["prev_text"] = ordered[idx - 1].get("text") if idx > 0 else None
    shaped["next_text"] = ordered[idx + 1].get("text") if idx < len(ordered) - 1 else None
    return shaped
```

- [ ] **Step 5: Implement `guidelines.py`**

Create `backend/mcp_server/guidelines.py`:

```python
AUTHORING_GUIDELINES = """\
Dialogue (text / 台词):
- Write in the SAME language as the project's existing dialogue / theme.
- Word-count targets by shot_duration (English-word approximation; advisory, not blocking):
  4s → 8-10 words, 6s → 13-16 words, 8s → 18-21 words.
- Keep it natural, in the character's voice (personality is implied by the reference images).

Motion (motion_prompt / 动作):
- Write the motion prompt in ENGLISH.
- Describe camera movement and talking-head physiological cues; preserve visual fidelity.
- If the shot has dialogue, the lip-sync marker (The character says: \"...\") is kept in sync
  automatically when you use update_motion with sync_lip_marker=true.

Storyboard:
- Storyboard JSON shots carry structure + dialogue only (no motion_prompt).
- Set motion_prompt afterward via update_motion / batch_update_shots.
- replace_storyboard requires the project to be in script_review status.
"""
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run --project backend pytest backend/tests/mcp/test_validation.py backend/tests/mcp/test_context.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/mcp_server/validation.py backend/mcp_server/context.py backend/mcp_server/guidelines.py backend/tests/mcp/test_validation.py backend/tests/mcp/test_context.py
git commit -m "feat(mcp): validation, context-shaping, and guidelines helpers"
```

---

## Task 6: MCP server — read tools

**Files:**
- Create: `backend/mcp_server/server.py`
- Test: `backend/tests/mcp/test_read_tools.py`

**Interfaces:**
- Consumes: `BackendClient`, `shape_project`, `shape_shot`, `with_neighbors`, `AUTHORING_GUIDELINES`, `settings`.
- Produces: `create_server(backend: BackendClient) -> FastMCP` registering read tools `list_projects`, `get_project`, `list_shots`, `get_shot`, `get_authoring_guidelines`. Tested via `fastmcp.Client(server)` in-memory. Also `main()` entrypoint (run later in Task 8).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/mcp/test_read_tools.py`:

```python
import json
import pytest
from fastmcp import Client
from tests.mcp.conftest import seed_project


def _payload(result):
    """Extract the structured/text payload from a FastMCP tool result."""
    if getattr(result, "data", None) is not None:
        return result.data
    return json.loads(result.content[0].text)


@pytest.fixture
def server(backend):
    from mcp_server.server import create_server
    return create_server(backend)


async def test_list_projects_tool(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        res = await c.call_tool("list_projects", {})
    ids = [p["id"] for p in _payload(res)]
    assert pid in ids


async def test_get_project_tool(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        res = await c.call_tool("get_project", {"project_id": pid})
    data = _payload(res)
    assert data["id"] == pid
    assert data["shot_count"] == 3
    assert "theme" in data


async def test_list_shots_tool(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        res = await c.call_tool("list_shots", {"project_id": pid})
    shots = _payload(res)
    assert [s["shot_id"] for s in shots] == [1, 2, 3]
    assert shots[0]["word_count_target"] == [13, 16]  # duration 6


async def test_get_shot_tool_with_neighbors(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        res = await c.call_tool("get_shot", {"project_id": pid, "shot_id": 2})
    data = _payload(res)
    assert data["shot_id"] == 2
    assert data["prev_text"] == "line 1"
    assert data["next_text"] == "line 3"


async def test_guidelines_tool(server):
    async with Client(server) as c:
        res = await c.call_tool("get_authoring_guidelines", {})
    text = _payload(res)
    assert "motion_prompt" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project backend pytest backend/tests/mcp/test_read_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_server.server'`.

- [ ] **Step 3: Implement the server with read tools**

Create `backend/mcp_server/server.py`:

```python
from fastmcp import FastMCP

from mcp_server.client import BackendClient
from mcp_server.config import settings
from mcp_server.context import shape_project, shape_shot, with_neighbors
from mcp_server.guidelines import AUTHORING_GUIDELINES


def create_server(backend: BackendClient) -> FastMCP:
    mcp = FastMCP("video-maker-dialogue-action")

    @mcp.tool
    async def list_projects() -> list[dict]:
        """List projects (id, title, status, shot_count)."""
        projects = await backend.list_projects()
        return [
            {
                "id": p["id"],
                "title": p.get("title"),
                "status": p.get("status"),
                "shot_count": p.get("shot_count", len(p.get("shots", []))),
            }
            for p in projects
        ]

    @mcp.tool
    async def get_project(project_id: str) -> dict:
        """Get project context: theme, status, characters, scene_overview, shot_count."""
        return shape_project(await backend.get_project(project_id))

    @mcp.tool
    async def list_shots(project_id: str) -> list[dict]:
        """List all shots of a project with dialogue, motion, and word-count info."""
        p = await backend.get_project(project_id)
        return [shape_shot(s) for s in sorted(p.get("shots", []), key=lambda s: s["shot_id"])]

    @mcp.tool
    async def get_shot(project_id: str, shot_id: int) -> dict:
        """Get one shot with prev/next dialogue context and word-count target."""
        p = await backend.get_project(project_id)
        return with_neighbors(p.get("shots", []), shot_id)

    @mcp.tool
    async def get_authoring_guidelines() -> str:
        """Return dialogue + motion authoring conventions."""
        return AUTHORING_GUIDELINES

    return mcp


def main() -> None:
    backend = BackendClient(base_url=settings.backend_base_url)
    server = create_server(backend)
    server.run(transport="http", host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project backend pytest backend/tests/mcp/test_read_tools.py -v`
Expected: all 5 PASS. (If `_payload` extraction fails for your installed `fastmcp` version, print `res` once to confirm whether the payload is on `res.data` or `res.content[0].text` and keep the helper — it already handles both.)

- [ ] **Step 5: Commit**

```bash
git add backend/mcp_server/server.py backend/tests/mcp/test_read_tools.py
git commit -m "feat(mcp): server scaffold + read tools"
```

---

## Task 7: MCP server — write tools

**Files:**
- Modify: `backend/mcp_server/server.py` (register write tools inside `create_server`)
- Test: `backend/tests/mcp/test_write_tools.py`

**Interfaces:**
- Consumes: `BackendClient.patch_shot`, `BackendClient.replace_storyboard`, `word_count_report`, `app.agents.director.postprocess_motion_prompt`.
- Produces: tools
  - `update_dialogue(project_id, shot_id, text) -> dict` → `{shot, word_count}` (rejects empty text)
  - `update_motion(project_id, shot_id, motion_prompt, sync_lip_marker=True) -> dict`
  - `batch_update_shots(project_id, updates: list[dict]) -> dict` → `{results: [{shot_id, ok, error?}]}`
  - `replace_storyboard(project_id, scene_overview, shots: list[dict]) -> dict`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/mcp/test_write_tools.py`:

```python
import json
import pytest
from fastmcp import Client
from tests.mcp.conftest import seed_project


def _payload(result):
    if getattr(result, "data", None) is not None:
        return result.data
    return json.loads(result.content[0].text)


@pytest.fixture
def server(backend):
    from mcp_server.server import create_server
    return create_server(backend)


async def test_update_dialogue(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        res = await c.call_tool("update_dialogue",
                                {"project_id": pid, "shot_id": 1, "text": "new dialogue here"})
    data = _payload(res)
    assert data["shot"]["text"] == "new dialogue here"
    assert "within_range" in data["word_count"]


async def test_update_dialogue_rejects_empty(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        with pytest.raises(Exception):
            await c.call_tool("update_dialogue",
                              {"project_id": pid, "shot_id": 1, "text": "   "})


async def test_update_motion_appends_lip_marker(server, db_session_factory):
    pid = await seed_project(db_session_factory)  # shot 1 text = "line 1"
    async with Client(server) as c:
        res = await c.call_tool("update_motion",
                                {"project_id": pid, "shot_id": 1,
                                 "motion_prompt": "slow zoom in", "sync_lip_marker": True})
    data = _payload(res)
    assert "slow zoom in" in data["shot"]["motion_prompt"]
    assert 'The character says: "line 1"' in data["shot"]["motion_prompt"]


async def test_update_motion_no_marker_when_disabled(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        res = await c.call_tool("update_motion",
                                {"project_id": pid, "shot_id": 1,
                                 "motion_prompt": "pan left", "sync_lip_marker": False})
    data = _payload(res)
    assert data["shot"]["motion_prompt"] == "pan left"


async def test_batch_update_shots_partial(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        res = await c.call_tool("batch_update_shots", {
            "project_id": pid,
            "updates": [
                {"shot_id": 1, "text": "t1", "motion_prompt": "m1"},
                {"shot_id": 999, "text": "bad"},  # nonexistent → fails this item only
            ],
        })
    results = _payload(res)["results"]
    by_id = {r["shot_id"]: r for r in results}
    assert by_id[1]["ok"] is True
    assert by_id[999]["ok"] is False


async def test_replace_storyboard_tool(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        res = await c.call_tool("replace_storyboard", {
            "project_id": pid,
            "scene_overview": "fresh",
            "shots": [
                {"shot_id": 1, "text": "only one", "shot_type": "Close-up",
                 "visual_description": "v", "shot_duration": 4, "align_with_previous": False},
            ],
        })
    data = _payload(res)
    assert data["ok"] is True
    # verify via read
    async with Client(server) as c:
        shots = _payload(await c.call_tool("list_shots", {"project_id": pid}))
    assert [s["shot_id"] for s in shots] == [1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project backend pytest backend/tests/mcp/test_write_tools.py -v`
Expected: FAIL — `Unknown tool: update_dialogue` (tools not registered yet).

- [ ] **Step 3: Add write tools to `create_server`**

In `backend/mcp_server/server.py`, add imports at the top:

```python
from app.agents.director import postprocess_motion_prompt
from mcp_server.client import BackendError
from mcp_server.validation import word_count_report
```

Then, inside `create_server` (before `return mcp`), register:

```python
    @mcp.tool
    async def update_dialogue(project_id: str, shot_id: int, text: str) -> dict:
        """Set a shot's dialogue (text/台词). Rejects empty text; word count is advisory."""
        if not text or not text.strip():
            raise ValueError("text must not be empty")
        shot = await backend.patch_shot(project_id, shot_id, {"text": text})
        return {
            "shot": shot,
            "word_count": word_count_report(text, shot["shot_duration"]),
            "note": _video_note(shot),
        }

    @mcp.tool
    async def update_motion(
        project_id: str, shot_id: int, motion_prompt: str, sync_lip_marker: bool = True
    ) -> dict:
        """Set a shot's motion_prompt (动作). When sync_lip_marker, keep the lip-sync line in sync."""
        final = motion_prompt
        if sync_lip_marker:
            current = await backend.get_project(project_id)
            shot_text = next(
                (s.get("text") for s in current.get("shots", []) if s["shot_id"] == shot_id),
                None,
            )
            if shot_text:
                final = postprocess_motion_prompt(motion_prompt, shot_text)
        shot = await backend.patch_shot(project_id, shot_id, {"motion_prompt": final})
        return {"shot": shot, "note": _video_note(shot)}

    @mcp.tool
    async def batch_update_shots(project_id: str, updates: list[dict]) -> dict:
        """Apply many {shot_id, text?, motion_prompt?} edits in one call. Partial success allowed."""
        results = []
        for u in updates:
            sid = u["shot_id"]
            body = {k: u[k] for k in ("text", "motion_prompt") if k in u and u[k] is not None}
            try:
                if not body:
                    raise ValueError("no text or motion_prompt provided")
                shot = await backend.patch_shot(project_id, sid, body)
                results.append({"shot_id": sid, "ok": True, "shot": shot})
            except (BackendError, ValueError) as e:
                results.append({"shot_id": sid, "ok": False, "error": str(e)})
        return {"results": results}

    @mcp.tool
    async def replace_storyboard(
        project_id: str, scene_overview: str, shots: list[dict]
    ) -> dict:
        """Full-replace the storyboard (structure + dialogue). Requires script_review status.

        Each shot: {shot_id, text, shot_type, visual_description, shot_duration,
        align_with_previous, reference_image_hint?}. Set motion via update_motion afterward.
        """
        try:
            await backend.replace_storyboard(project_id, scene_overview, shots)
            return {"ok": True}
        except BackendError as e:
            return {"ok": False, "status_code": e.status_code, "error": e.detail}
```

Add this helper at module level (below `create_server`):

```python
def _video_note(shot: dict) -> str | None:
    if shot.get("video_path"):
        return "edit saved; it won't change the existing video until the shot is regenerated"
    return None
```

Note: the `update_motion` / `update_dialogue` PATCH responses come from `patch_shot`, which returns the handler's dict (includes `shot_duration`, `text`, `motion_prompt`, but **not** `video_path`). `_video_note` therefore returns `None` from those responses — acceptable; the has_video signal is available via `get_shot`/`list_shots`. Keep `_video_note` for forward-compatibility.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --project backend pytest backend/tests/mcp/test_write_tools.py -v`
Expected: all 6 PASS.

- [ ] **Step 5: Run the full MCP + pipeline suite**

Run: `uv run --project backend pytest backend/tests/mcp backend/tests/integration/test_pipeline.py -v`
Expected: all PASS (no regressions).

- [ ] **Step 6: Commit**

```bash
git add backend/mcp_server/server.py backend/tests/mcp/test_write_tools.py
git commit -m "feat(mcp): write tools (dialogue, motion, batch, replace storyboard)"
```

---

## Task 8: Compose service + Makefile + smoke test

**Files:**
- Modify: `deploy/docker-compose.dev.yml` (add `mcp` service)
- Modify: `Makefile` (add `dev-mcp`)

**Interfaces:**
- Consumes: backend image `video-maker-worker-dev`, backend service `video-maker-backend-dev:8002`.
- Produces: a running `mcp` container serving HTTP on host port `8765`.

- [ ] **Step 1: Add the `mcp` service**

In `deploy/docker-compose.dev.yml`, add after the `vc-worker` service block (mirrors `worker`, minus LLM secrets, plus a published port and backend URL):

```yaml
  mcp:
    build:
      context: .
      dockerfile: Dockerfile.worker
    image: video-maker-worker-dev
    container_name: video-maker-mcp-dev
    ports:
      - "8765:8765"
    volumes:
      - ../backend:/app:z
      - uv-cache:/root/.cache/uv
      - backend-venv:/app/.venv
    env_file:
      - path: ./config.env
        required: true
    working_dir: /app
    environment:
      BACKEND_BASE_URL: http://video-maker-backend-dev:8002
      MCP_HOST: 0.0.0.0
      MCP_PORT: "8765"
    command: >
      uv run --project . python -m mcp_server.server
    depends_on:
      - backend
    restart: unless-stopped
```

- [ ] **Step 2: Add the Makefile target**

In `Makefile`, add `dev-mcp` to the `.PHONY` line (with the other `dev-*` targets), add a help line near the other `dev-*` help echoes, and add the target near `dev-worker`:

```makefile
dev-mcp:
	@echo "Starting MCP server..."
	podman compose -f $(DEV_COMPOSE) up -d mcp
```

- [ ] **Step 3: Validate compose config**

Run: `podman compose -f deploy/docker-compose.dev.yml config >/dev/null && echo OK`
Expected: prints `OK` (compose file parses, `mcp` service valid).

- [ ] **Step 4: Smoke-test the running service**

Run (brings up stack + mcp, then probes the MCP HTTP endpoint):
```bash
make dev-backend && make dev-mcp && sleep 5 && \
  curl -sS -o /dev/null -w "%{http_code}\n" http://localhost:8765/mcp/
```
Expected: an HTTP status (e.g. `400`/`406`/`200` depending on handshake headers) — **not** a connection refused — confirming the MCP server is listening. Then check logs:
`podman logs video-maker-mcp-dev --tail 20` shows the FastMCP server started on `0.0.0.0:8765`.

If the endpoint path differs for your `fastmcp` version, confirm the mount path from the startup log line and adjust the smoke probe accordingly.

- [ ] **Step 5: Commit**

```bash
git add deploy/docker-compose.dev.yml Makefile
git commit -m "feat(mcp): add mcp compose service + dev-mcp make target"
```

---

## Task 9: Documentation

**Files:**
- Modify: `backend/docs/backend/ARCHTECH.md` (or the nearest existing backend doc; create `backend/mcp_server/README.md` if no fit)

**Interfaces:** none (docs only).

- [ ] **Step 1: Document the endpoint + MCP**

Add a section covering:
- `PUT /api/projects/{id}/storyboard` — full-replace semantics, `script_review`-only, JSON shape, file-cleanup behavior.
- The `mcp` service — purpose (agent-authored 台词/动作), transport (HTTP on `:8765`), `BACKEND_BASE_URL`, no auth (trusted network).
- Tool catalog: `list_projects`, `get_project`, `list_shots`, `get_shot`, `get_authoring_guidelines`, `update_dialogue`, `update_motion`, `batch_update_shots`, `replace_storyboard` — one line each with args.
- Two-phase flow: `replace_storyboard` (structure + dialogue) → `batch_update_shots` (motion).

- [ ] **Step 2: Commit**

```bash
git add -A
git commit -m "docs(mcp): document storyboard PUT endpoint and MCP tools"
```

---

## Self-Review

**1. Spec coverage**
- Architecture (compose service from backend image, httpx to backend) → Tasks 3, 8. ✓
- Code layout (`client/validation/context/guidelines/server`) → Tasks 4–7. ✓
- `PUT /storyboard` full-replace + validation + status guard + json rewrite → Task 1. ✓
- Material-file audit (delete leftover shot dir) → Task 2. ✓
- Two JSON contracts (storyboard `ShotItem` vs `ShotUpdate`) → Tasks 1, 4, 7. ✓
- 5 read + 4 write tools → Tasks 6, 7. ✓
- Word count advisory (reuse `validate_word_count`) → Task 5. ✓
- `has_video` note → Task 7 (`_video_note`). ✓
- Lip-sync marker via `postprocess_motion_prompt` → Task 7. ✓
- Error handling (BackendError, 409/404, batch partial) → Tasks 4, 7. ✓
- Testing (unit + backend integration + FastMCP in-memory) → all tasks. ✓
- Deploy wiring (compose, Makefile, no secrets) → Task 8. ✓
- Docs → Task 9. ✓

**2. Placeholder scan:** No TBD/TODO; every code step shows full code and exact commands. ✓

**3. Type consistency:** `BackendClient` methods (`list_projects`/`get_project`/`patch_shot`/`replace_storyboard`) and `BackendError(status_code, detail)` are defined in Task 4 and used identically in Tasks 6–7. `shape_project`/`shape_shot`/`with_neighbors`/`word_count_report` signatures match between Task 5 definitions and Task 6/7 usage. `create_server(backend)` defined Task 6, extended Task 7, used by tests in both. `_payload()` helper duplicated intentionally across two test files (tasks may be implemented out of order). ✓

**Known follow-ups (out of scope, noted in spec):** existing `PATCH /storyboard` still doesn't rewrite `storyboard.json` (only the new `PUT` does); aligning `PATCH` is deferred.
