# MCP Lifecycle Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `docs/frds/mcp_lifecycle_tools_frd.md` — add 6 lifecycle MCP tools (start/approve/regenerate/continue/cancel/status) and extend `batch_update_shots` with `visual_description`, so an agent can drive a project from draft to shot completion purely over MCP.

**Architecture:** All new tools are thin pass-throughs in `backend/mcp_server/server.py` over new one-line wrappers in `backend/mcp_server/client.py` (which injects `X-User-Name` and base_url). Error handling copies the existing `replace_storyboard` pattern: catch `BackendError`, return `{"ok": false, "status_code", "error"}`. `get_generation_status` reuses `backend.get_project` with a new shape function in `context.py` (existing `shape_project` untouched). No backend route/state-machine changes.

**Tech Stack:** Python 3.12, FastMCP, httpx, pytest (+anyio), SQLAlchemy async over in-memory SQLite (existing `tests/mcp/conftest.py` runs the real backend ASGI app; only arq/redis are mocked).

## Global Constraints

- Never run `python` directly — tests run as `uv run --project backend pytest ...` (memory: uv直跑, not podman)
- No hardcoded absolute paths anywhere
- Driver tools (FR-1~FR-5) are **async-trigger semantics**: enqueue and return immediately; docstring must say to poll `get_generation_status`
- Success responses pass the backend body through **原样** (`{"status": "queued", "message": ...}`); errors are `{"ok": false, "status_code": <int>, "error": <detail>}` — never a raised exception crossing the MCP boundary for backend 4xx
- Existing tools must not change behavior (FRD acceptance #4): don't touch `shape_project`, don't add validation to existing `text`/`motion_prompt` batch paths
- Backend facts (verified): all five endpoints return `{status, message}`-shaped JSON with 202; invalid transition → 409 `{"detail": ...}`; `/start` without a character reference image → 400; `/continue-generation` outside `shot_review` → 409, with no pending shots → 400. `ShotUpdate` already accepts `visual_description`. `ShotResponse` already exposes `status/video_path/error_message/vc_status/tf_status`.

## File Structure

- `backend/mcp_server/client.py` — +5 wrapper methods (start_project, approve_script, regenerate_shots, continue_generation, cancel_generation)
- `backend/mcp_server/context.py` — +`shape_generation_status(p)`
- `backend/mcp_server/server.py` — +6 `@mcp.tool`s, +`_lifecycle_error` helper, batch whitelist extension
- `backend/mcp_server/authoring_skill.md` — +«## 6. 生命周期工具» section (guidelines.py serves this file verbatim)
- `backend/tests/mcp/test_client.py` — client wrapper tests
- `backend/tests/mcp/test_lifecycle_tools.py` — NEW: MCP-level tests for the 6 tools
- `backend/tests/mcp/test_context.py` — shape_generation_status tests
- `backend/tests/mcp/test_write_tools.py` — batch visual_description tests

---

### Task 1: BackendClient lifecycle wrappers

**Files:**
- Modify: `backend/mcp_server/client.py` (append after `replace_storyboard`)
- Test: `backend/tests/mcp/test_client.py`

**Interfaces:**
- Produces: `BackendClient.start_project(project_id) -> dict`, `.approve_script(project_id) -> dict`, `.regenerate_shots(project_id, shot_ids: list[int]) -> dict`, `.continue_generation(project_id) -> dict`, `.cancel_generation(project_id) -> dict`. All raise `BackendError(status_code, detail)` on 4xx/5xx.

- [ ] **Step 1: Write failing tests** — append to `backend/tests/mcp/test_client.py`:

```python
async def test_client_start_project_requires_character_image(backend, db_session_factory):
    from mcp_server.client import BackendError
    pid = await seed_project(db_session_factory, status="draft")
    with pytest.raises(BackendError) as ei:
        await backend.start_project(pid)
    assert ei.value.status_code == 400


async def test_client_start_project_queues(backend, db_session_factory):
    pid = await seed_project(db_session_factory, status="draft")
    await seed_reference_image(db_session_factory, pid)
    res = await backend.start_project(pid)
    assert res["status"] == "queued"


async def test_client_approve_script(backend, db_session_factory):
    pid = await seed_project(db_session_factory, status="script_review")
    res = await backend.approve_script(pid)
    assert res["status"] == "queued"


async def test_client_regenerate_shots(backend, db_session_factory):
    pid = await seed_project(db_session_factory, status="shot_review")
    res = await backend.regenerate_shots(pid, [1, 2])
    assert res["status"] == "queued"


async def test_client_continue_generation(backend, db_session_factory):
    pid = await seed_project(db_session_factory, status="shot_review")
    res = await backend.continue_generation(pid)
    assert res["status"] == "queued"


async def test_client_cancel_generation(backend, db_session_factory):
    pid = await seed_project(db_session_factory, status="shot_generating")
    res = await backend.cancel_generation(pid)
    assert res["status"] == "shot_review"
```

Requires a new seed helper in `backend/tests/mcp/conftest.py` (after `seed_project`):

```python
async def seed_reference_image(sf, project_id, kind="character"):
    from app.models.project import ReferenceImage
    async with sf() as s:
        s.add(ReferenceImage(project_id=project_id, kind=kind,
                             filename="ref.png", storage_path="refs/ref.png",
                             order_index=0))
        await s.commit()
```

Import it in test files: `from tests.mcp.conftest import seed_project, seed_reference_image`.

- [ ] **Step 2: Run to verify failure** — `uv run --project backend pytest tests/mcp/test_client.py -x -q` (run from `backend/`). Expected: FAIL `AttributeError: 'BackendClient' object has no attribute 'start_project'`.

- [ ] **Step 3: Implement** — append to `BackendClient` in `backend/mcp_server/client.py`:

```python
    # ── lifecycle drivers ────────────────────────────────────────────────

    async def start_project(self, project_id: str) -> dict:
        return await self._request("POST", f"/api/projects/{project_id}/start")

    async def approve_script(self, project_id: str) -> dict:
        return await self._request("POST", f"/api/projects/{project_id}/approve-script")

    async def regenerate_shots(self, project_id: str, shot_ids: list[int]) -> dict:
        return await self._request(
            "POST", f"/api/projects/{project_id}/regenerate-shots",
            json={"shot_ids": shot_ids},
        )

    async def continue_generation(self, project_id: str) -> dict:
        return await self._request("POST", f"/api/projects/{project_id}/continue-generation")

    async def cancel_generation(self, project_id: str) -> dict:
        return await self._request("POST", f"/api/projects/{project_id}/cancel-generation")
```

- [ ] **Step 4: Verify pass** — `uv run --project backend pytest tests/mcp/test_client.py -q`. Expected: all PASS.

- [ ] **Step 5: Commit** — `git add backend/mcp_server/client.py backend/tests/mcp/test_client.py backend/tests/mcp/conftest.py && git commit -m "feat(mcp): add lifecycle wrapper methods to BackendClient"`

---

### Task 2: Lifecycle driver MCP tools (FR-1 ~ FR-5)

**Files:**
- Modify: `backend/mcp_server/server.py` (new tools after `replace_storyboard`, helper near `_video_note`)
- Test: `backend/tests/mcp/test_lifecycle_tools.py` (new)

**Interfaces:**
- Consumes: Task 1's five `BackendClient` methods.
- Produces: MCP tools `start_generation(project_id)`, `approve_script(project_id)`, `regenerate_shots(project_id, shot_ids)`, `continue_generation(project_id)`, `cancel_generation(project_id)`. Success → backend body as-is; `BackendError` → `{"ok": False, "status_code", "error"}`; bad args → `ValueError`.

- [ ] **Step 1: Write failing tests** — create `backend/tests/mcp/test_lifecycle_tools.py`:

```python
import json
import pytest
from fastmcp import Client

from tests.mcp.conftest import seed_project, seed_reference_image


def _payload(result):
    if getattr(result, "data", None) is not None:
        return result.data
    return json.loads(result.content[0].text)


@pytest.fixture
def server(backend):
    from mcp_server.server import create_server
    return create_server(backend)


async def test_start_generation_queues(server, db_session_factory):
    pid = await seed_project(db_session_factory, status="draft")
    await seed_reference_image(db_session_factory, pid)
    async with Client(server) as c:
        res = await c.call_tool("start_generation", {"project_id": pid})
    assert _payload(res)["status"] == "queued"


async def test_start_generation_without_character_image_is_structured_error(
    server, db_session_factory
):
    pid = await seed_project(db_session_factory, status="draft")
    async with Client(server) as c:
        res = await c.call_tool("start_generation", {"project_id": pid})
    data = _payload(res)
    assert data["ok"] is False
    assert data["status_code"] == 400
    assert "character" in data["error"]


async def test_approve_script_queues(server, db_session_factory):
    pid = await seed_project(db_session_factory, status="script_review")
    async with Client(server) as c:
        res = await c.call_tool("approve_script", {"project_id": pid})
    assert _payload(res)["status"] == "queued"


async def test_approve_script_wrong_status_409(server, db_session_factory):
    pid = await seed_project(db_session_factory, status="draft")
    async with Client(server) as c:
        res = await c.call_tool("approve_script", {"project_id": pid})
    data = _payload(res)
    assert data["ok"] is False
    assert data["status_code"] == 409


async def test_regenerate_shots_queues(server, db_session_factory):
    pid = await seed_project(db_session_factory, status="shot_review")
    async with Client(server) as c:
        res = await c.call_tool("regenerate_shots", {"project_id": pid, "shot_ids": [1, 3]})
    assert _payload(res)["status"] == "queued"


async def test_regenerate_shots_rejects_empty_list(server, db_session_factory):
    pid = await seed_project(db_session_factory, status="shot_review")
    async with Client(server) as c:
        with pytest.raises(Exception, match="shot_ids"):
            await c.call_tool("regenerate_shots", {"project_id": pid, "shot_ids": []})


async def test_regenerate_shots_rejects_non_positive_ids(server, db_session_factory):
    pid = await seed_project(db_session_factory, status="shot_review")
    async with Client(server) as c:
        with pytest.raises(Exception, match="positive"):
            await c.call_tool("regenerate_shots", {"project_id": pid, "shot_ids": [1, 0]})


async def test_continue_generation_queues(server, db_session_factory):
    pid = await seed_project(db_session_factory, status="shot_review")
    async with Client(server) as c:
        res = await c.call_tool("continue_generation", {"project_id": pid})
    assert _payload(res)["status"] == "queued"


async def test_continue_generation_wrong_status_409(server, db_session_factory):
    pid = await seed_project(db_session_factory, status="draft")
    async with Client(server) as c:
        res = await c.call_tool("continue_generation", {"project_id": pid})
    data = _payload(res)
    assert data["ok"] is False
    assert data["status_code"] == 409


async def test_cancel_generation_returns_to_shot_review(server, db_session_factory):
    pid = await seed_project(db_session_factory, status="shot_generating")
    async with Client(server) as c:
        res = await c.call_tool("cancel_generation", {"project_id": pid})
    assert _payload(res)["status"] == "shot_review"


async def test_cancel_generation_wrong_status_409(server, db_session_factory):
    pid = await seed_project(db_session_factory, status="draft")
    async with Client(server) as c:
        res = await c.call_tool("cancel_generation", {"project_id": pid})
    data = _payload(res)
    assert data["ok"] is False
    assert data["status_code"] == 409
```

- [ ] **Step 2: Verify failure** — `uv run --project backend pytest tests/mcp/test_lifecycle_tools.py -x -q`. Expected: FAIL, unknown tool `start_generation`.

- [ ] **Step 3: Implement** — in `backend/mcp_server/server.py`, add module-level helper next to `_video_note`:

```python
def _backend_err(e: BackendError) -> dict:
    return {"ok": False, "status_code": e.status_code, "error": e.detail}
```

and inside `create_server`, after `replace_storyboard`:

```python
    @mcp.tool
    async def start_generation(project_id: str) -> dict:
        """Start script generation (draft → scripting). Requires ≥1 uploaded
        character reference image. Async trigger: queues the screenwriter and
        returns immediately — poll get_generation_status until status becomes
        script_review. Errors return {"ok": false, "status_code", "error"}."""
        try:
            return await backend.start_project(project_id)
        except BackendError as e:
            return _backend_err(e)

    @mcp.tool
    async def approve_script(project_id: str) -> dict:
        """Approve the script and start shot generation (script_review →
        shot_generating). Resets all shots to pending. Async trigger: returns
        immediately — poll get_generation_status until status becomes shot_review."""
        try:
            return await backend.approve_script(project_id)
        except BackendError as e:
            return _backend_err(e)

    @mcp.tool
    async def regenerate_shots(project_id: str, shot_ids: list[int]) -> dict:
        """Regenerate specific shots with their current prompts (shot_review →
        shot_generating). Async trigger: returns immediately — poll
        get_generation_status until the shots report has_video=true."""
        if not shot_ids:
            raise ValueError("shot_ids must not be empty")
        if any(sid <= 0 for sid in shot_ids):
            raise ValueError("shot_ids must all be positive integers")
        try:
            return await backend.regenerate_shots(project_id, shot_ids)
        except BackendError as e:
            return _backend_err(e)

    @mcp.tool
    async def continue_generation(project_id: str) -> dict:
        """Resume generation of the next pending/failed shot (shot_review →
        shot_generating). Async trigger: returns immediately — poll
        get_generation_status for progress."""
        try:
            return await backend.continue_generation(project_id)
        except BackendError as e:
            return _backend_err(e)

    @mcp.tool
    async def cancel_generation(project_id: str) -> dict:
        """Cancel in-flight shot generation and return the project to
        shot_review. In-progress shots are reset to pending."""
        try:
            return await backend.cancel_generation(project_id)
        except BackendError as e:
            return _backend_err(e)
```

- [ ] **Step 4: Verify pass** — `uv run --project backend pytest tests/mcp/test_lifecycle_tools.py -q`. Expected: all PASS.

- [ ] **Step 5: Commit** — `git add backend/mcp_server/server.py backend/tests/mcp/test_lifecycle_tools.py && git commit -m "feat(mcp): add lifecycle driver tools (start/approve/regenerate/continue/cancel)"`

---

### Task 3: get_generation_status (FR-6)

**Files:**
- Modify: `backend/mcp_server/context.py` (append), `backend/mcp_server/server.py` (new tool after `cancel_generation`)
- Test: `backend/tests/mcp/test_context.py`, `backend/tests/mcp/test_lifecycle_tools.py`

**Interfaces:**
- Produces: `shape_generation_status(p: dict) -> dict` and MCP tool `get_generation_status(project_id)` returning `{"status", "shots": [{"shot_id", "status", "has_video", "video_path", "error_message", "vc_status", "tf_status"}]}`, shots ordered by shot_id.

- [ ] **Step 1: Write failing tests.** Append to `backend/tests/mcp/test_context.py`:

```python
def test_shape_generation_status():
    from mcp_server.context import shape_generation_status
    p = {
        "status": "shot_review",
        "shots": [
            {"shot_id": 2, "status": "pending", "video_path": None,
             "error_message": "boom", "vc_status": None, "tf_status": None},
            {"shot_id": 1, "status": "completed", "video_path": "projects/x/1/output.mp4",
             "error_message": None, "vc_status": "completed", "tf_status": None},
        ],
    }
    out = shape_generation_status(p)
    assert out["status"] == "shot_review"
    assert [s["shot_id"] for s in out["shots"]] == [1, 2]
    assert out["shots"][0] == {
        "shot_id": 1, "status": "completed", "has_video": True,
        "video_path": "projects/x/1/output.mp4", "error_message": None,
        "vc_status": "completed", "tf_status": None,
    }
    assert out["shots"][1]["has_video"] is False
    assert out["shots"][1]["error_message"] == "boom"
```

Append to `backend/tests/mcp/test_lifecycle_tools.py`:

```python
async def test_get_generation_status(server, db_session_factory):
    from sqlalchemy import select
    from app.models.project import Shot
    pid = await seed_project(db_session_factory, status="shot_review")
    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(
            Shot.project_id == pid, Shot.shot_id == 1))).scalar_one()
        shot.status = "completed"
        shot.video_path = f"projects/{pid}/1/output.mp4"
        await s.commit()
    async with Client(server) as c:
        res = await c.call_tool("get_generation_status", {"project_id": pid})
    data = _payload(res)
    assert data["status"] == "shot_review"
    assert len(data["shots"]) == 3
    first = data["shots"][0]
    assert first["shot_id"] == 1
    assert first["has_video"] is True
    assert first["video_path"] == f"projects/{pid}/1/output.mp4"
    assert set(first) == {"shot_id", "status", "has_video", "video_path",
                          "error_message", "vc_status", "tf_status"}
```

- [ ] **Step 2: Verify failure** — `uv run --project backend pytest tests/mcp/test_context.py tests/mcp/test_lifecycle_tools.py -x -q`. Expected: FAIL `ImportError ... shape_generation_status`.

- [ ] **Step 3: Implement.** Append to `backend/mcp_server/context.py`:

```python
def shape_generation_status(p: dict) -> dict:
    return {
        "status": p["status"],
        "shots": [
            {
                "shot_id": s["shot_id"],
                "status": s.get("status"),
                "has_video": bool(s.get("video_path")),
                "video_path": s.get("video_path"),
                "error_message": s.get("error_message"),
                "vc_status": s.get("vc_status"),
                "tf_status": s.get("tf_status"),
            }
            for s in sorted(p.get("shots", []), key=lambda s: s["shot_id"])
        ],
    }
```

In `backend/mcp_server/server.py`: extend the context import to include `shape_generation_status`, and add after `cancel_generation`:

```python
    @mcp.tool
    async def get_generation_status(project_id: str) -> dict:
        """Poll generation progress: project status plus per-shot status,
        video_path, error_message, vc_status and tf_status. Use after any
        driver tool (start_generation / approve_script / regenerate_shots /
        continue_generation) to watch the async pipeline."""
        return shape_generation_status(await backend.get_project(project_id))
```

- [ ] **Step 4: Verify pass** — same command as Step 2. Expected: all PASS.

- [ ] **Step 5: Commit** — `git add backend/mcp_server/context.py backend/mcp_server/server.py backend/tests/mcp/test_context.py backend/tests/mcp/test_lifecycle_tools.py && git commit -m "feat(mcp): add get_generation_status polling tool"`

---

### Task 4: batch_update_shots visual_description (FR-7)

**Files:**
- Modify: `backend/mcp_server/server.py` (`batch_update_shots` body)
- Test: `backend/tests/mcp/test_write_tools.py`

**Interfaces:**
- Produces: `batch_update_shots` accepts `visual_description` in each update dict; empty/whitespace-only string → per-item `{"ok": False, "error": ...}` (partial success preserved). Existing `text`/`motion_prompt` behavior unchanged.

- [ ] **Step 1: Write failing tests** — append to `backend/tests/mcp/test_write_tools.py`:

```python
async def test_batch_update_supports_visual_description(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        res = await c.call_tool("batch_update_shots", {
            "project_id": pid,
            "updates": [{"shot_id": 1, "visual_description": "new look"}],
        })
    results = _payload(res)["results"]
    assert results[0]["ok"] is True
    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(
            Shot.project_id == pid, Shot.shot_id == 1))).scalar_one()
    assert shot.visual_description == "new look"


async def test_batch_update_rejects_empty_visual_description(server, db_session_factory):
    pid = await seed_project(db_session_factory)
    async with Client(server) as c:
        res = await c.call_tool("batch_update_shots", {
            "project_id": pid,
            "updates": [{"shot_id": 1, "visual_description": "  "},
                        {"shot_id": 2, "text": "still fine"}],
        })
    results = _payload(res)["results"]
    assert results[0]["ok"] is False
    assert "visual_description" in results[0]["error"]
    assert results[1]["ok"] is True
```

- [ ] **Step 2: Verify failure** — `uv run --project backend pytest tests/mcp/test_write_tools.py -x -q -k visual_description`. Expected: first test FAILs — `visual_description` filtered out, so error "no text or motion_prompt provided".

- [ ] **Step 3: Implement** — in `batch_update_shots`, replace:

```python
                body = {k: u[k] for k in ("text", "motion_prompt") if k in u and u[k] is not None}
                if not body:
                    raise ValueError("no text or motion_prompt provided")
```

with:

```python
                body = {
                    k: u[k]
                    for k in ("text", "motion_prompt", "visual_description")
                    if k in u and u[k] is not None
                }
                if not body:
                    raise ValueError("no text, motion_prompt or visual_description provided")
                vd = body.get("visual_description")
                if vd is not None and not vd.strip():
                    raise ValueError("visual_description must not be empty")
```

and update the docstring to `"""Apply many {shot_id, text?, motion_prompt?, visual_description?} edits in one call. Partial success allowed."""`.

- [ ] **Step 4: Verify pass** — `uv run --project backend pytest tests/mcp/test_write_tools.py -q`. Expected: all PASS (including pre-existing tests).

- [ ] **Step 5: Commit** — `git add backend/mcp_server/server.py backend/tests/mcp/test_write_tools.py && git commit -m "feat(mcp): allow visual_description in batch_update_shots"`

---

### Task 5: Lifecycle section in authoring guidelines

**Files:**
- Modify: `backend/mcp_server/authoring_skill.md` (append; `guidelines.py` serves this file verbatim — do not edit the string in guidelines.py)
- Test: `backend/tests/mcp/test_read_tools.py`

- [ ] **Step 1: Write failing test** — append to `backend/tests/mcp/test_read_tools.py` (reuse its existing `server`/`_payload` helpers):

```python
async def test_guidelines_document_lifecycle_tools(server):
    async with Client(server) as c:
        res = await c.call_tool("get_authoring_guidelines", {})
    text = res.content[0].text
    for name in ("start_generation", "approve_script", "regenerate_shots",
                 "continue_generation", "cancel_generation", "get_generation_status"):
        assert name in text
```

- [ ] **Step 2: Verify failure** — `uv run --project backend pytest tests/mcp/test_read_tools.py -x -q -k lifecycle`. Expected: FAIL on `start_generation` missing.

- [ ] **Step 3: Implement** — append to `backend/mcp_server/authoring_skill.md`:

```markdown
## 7. 生命周期工具（状态机）

项目状态机与对应驱动工具：

```
draft ──start_generation──▶ scripting ──▶ script_review
script_review ──(replace_storyboard / batch_update_shots 编辑)──▶ script_review
script_review ──approve_script──▶ shot_generating ──▶ shot_review
shot_review ──(regenerate_shots / 编辑)──▶ shot_generating ──▶ shot_review
shot_review ──continue_generation──▶ shot_generating（续跑 pending/failed shot）
shot_generating ──cancel_generation──▶ shot_review（止损）
shot_review ──export（暂无 MCP 工具，走 UI）──▶ exporting ──▶ exported
```

- `start_generation` 前置：项目为 draft 且已用 `upload_reference_images` 上传 ≥1 张 character 参考图。
- 驱动类工具（start_generation / approve_script / regenerate_shots / continue_generation / cancel_generation）为**异步触发**：只入队并立即返回 `{status, message}`；进度用 `get_generation_status` 轮询（返回项目 status 与每个 shot 的 status / has_video / video_path / error_message / vc_status / tf_status）。
- 非法状态下调用返回 `{"ok": false, "status_code": 409, "error": ...}`，属正常反馈而非连接错误。
```

（章节号顺延现有文档，若与现有编号冲突则用下一个空闲编号。）

- [ ] **Step 4: Verify pass** — `uv run --project backend pytest tests/mcp/test_read_tools.py -q`. Expected: all PASS.

- [ ] **Step 5: Commit** — `git add backend/mcp_server/authoring_skill.md backend/tests/mcp/test_read_tools.py && git commit -m "docs(mcp): document lifecycle state machine in authoring guidelines"`

---

### Task 6: Full regression + ship

- [ ] **Step 1:** `uv run --project backend pytest tests/mcp -q` — Expected: entire MCP suite PASS (acceptance #4: no regressions).
- [ ] **Step 2:** `uv run --project backend pytest -q` — run the whole backend suite; pre-existing failures unrelated to `mcp_server/` are acceptable but must be noted in the PR description.
- [ ] **Step 3:** Push branch, open draft PR against `master` with the FRD linked (`gh pr create --draft`).

## Self-Review Notes

- FRD FR-1→Task 2, FR-2→Task 2, FR-3→Task 2, FR-4/5→Task 2, FR-6→Task 3, FR-7→Task 4, guidelines requirement §3.3→Task 5, client-wrapper requirement §3.3→Task 1. Acceptance #2 (structured 409) covered by Task 2 tests; #3 (visual_description persists) by Task 4 test asserting the DB row; #4 by Task 6 full-suite run. Acceptance #1 (纯 MCP 全流程) is an operational check against the live stack — the ASGI-level tests cover every hop except the real worker, which bills; noted for the PR description.
- Empty-string rule: applied ONLY to the new `visual_description` field to honor acceptance #4 (existing text/motion batch semantics untouched).
- `regenerate_shots` MCP tool name intentionally shadows the backend route name; FastMCP scopes tool functions inside `create_server`, no collision with the client method (`backend.regenerate_shots`).
