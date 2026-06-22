# 临时 Join Shot 连贯性检测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户把当前选中的若干 shot 临时纯拼接成一条视频，在页面内弹窗播放，用于肉眼检测分镜连贯性。

**Architecture:** 后端新增一个**同步** FastAPI 端点 `POST /api/projects/{id}/join-preview`，校验选中的 shot 后调用现有 `merge_shots()`（concat demuxer，无转场、无重编码）输出到固定文件 `previews/join_preview.mp4`（每次覆盖），返回带 cache-busting 参数的 media URL。前端在批量操作区加按钮，调 API 后在 modal 里 `<video>` 播放。

**Tech Stack:** FastAPI + SQLAlchemy async（后端）、python-ffmpeg（拼接）、React + Vite + TypeScript（前端）、pytest（后端测试）、Playwright（前端测试）。

## Global Constraints

- 后端测试用 `uv run --project backend pytest` 直接运行，不走 podman。
- 只 mock 花钱的服务（LLM/模型）；ffmpeg 是本地免费工具，测试中用真实小 fixture 运行，不 mock。
- 不写硬编码绝对路径；Python 用 `pathlib` 相对 `__file__` 或 storage helper，TS 用相对路径。
- Google GenAI 无关（本功能不涉及）。
- 本功能**只读** `shot.video_path`，输出到独立 `previews/` 目录，不修改/重命名/删除任何 shot 素材 → 不触发 CLAUDE.md「素材文件变更审计」。
- 前端 Playwright 测试必须 mock `POST /api/projects/*/join-preview`（避免触发真实 ffmpeg）。
- 所有 shot 相关端点需 `X-User-Name` header（复用 `_require_user` 依赖）。

---

### Task 1: Storage helper `join_preview_path`

**Files:**
- Modify: `backend/app/services/storage.py`（在 `final_video_path` 附近，约 line 106-108 之后新增）
- Test: `backend/tests/unit/test_storage_join_preview.py`（Create）

**Interfaces:**
- Consumes: 现有 `project_dir(project_id: str) -> Path`、`settings.storage_root`。
- Produces: `join_preview_path(project_id: str) -> Path` —— 返回 `<storage_root>/projects/<project_id>/previews/join_preview.mp4`。函数内确保 `previews/` 目录存在（`mkdir(parents=True, exist_ok=True)`），与同文件其它 helper 风格一致。

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_storage_join_preview.py`:

```python
from pathlib import Path

from app.config import settings
from app.services import storage


def test_join_preview_path(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))

    p = storage.join_preview_path("proj-123")

    expected = tmp_path / "projects" / "proj-123" / "previews" / "join_preview.mp4"
    assert Path(p) == expected
    # parent dir is created so ffmpeg can write into it
    assert expected.parent.is_dir()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project backend pytest backend/tests/unit/test_storage_join_preview.py -v`
Expected: FAIL with `AttributeError: module 'app.services.storage' has no attribute 'join_preview_path'`

- [ ] **Step 3: Write minimal implementation**

In `backend/app/services/storage.py`, after `final_video_path` (around line 108), add:

```python
def join_preview_path(project_id: str) -> Path:
    """临时连贯性预览视频的固定输出路径（每次覆盖）。"""
    previews_dir = project_dir(project_id) / "previews"
    previews_dir.mkdir(parents=True, exist_ok=True)
    return previews_dir / "join_preview.mp4"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project backend pytest backend/tests/unit/test_storage_join_preview.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/storage.py backend/tests/unit/test_storage_join_preview.py
git commit -m "feat(storage): add join_preview_path helper for continuity preview"
```

---

### Task 2: 后端 `JoinPreviewRequest` schema + `POST /join-preview` 端点

**Files:**
- Modify: `backend/app/models/schemas.py`（在 `ExportRequest` 之后，约 line 184）
- Modify: `backend/app/api/pipeline.py`（imports 区 + 在 `export_project` 端点之后新增端点）
- Test: `backend/tests/integration/test_join_preview.py`（Create）

**Interfaces:**
- Consumes:
  - `storage.join_preview_path(project_id) -> Path`（Task 1）
  - 现有 `merge_shots(shot_paths: list[str], output_path: str) -> None`（`app.agents.merger`）
  - 现有 `to_media_url(absolute_path: Optional[str]) -> Optional[str]`（已在 pipeline.py import）
  - 现有依赖 `_require_user`、`get_session`、`_get_project_or_404`
  - 现有 `Shot` 模型字段：`shot_id: int`、`project_id: str`、`status: str`、`video_path: Optional[str]`
  - 现有 `ShotStatus.COMPLETED`（`.value` 为字符串，比较时用 `ShotStatus.COMPLETED.value`，与 `export_project` 一致）
- Produces:
  - `JoinPreviewRequest(BaseModel)` with field `shot_ids: list[int]`
  - 端点 `POST /api/projects/{project_id}/join-preview` 返回 JSON `{"preview_url": str}`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/integration/test_join_preview.py`:

```python
import subprocess
from pathlib import Path

import pytest

from app.config import settings
from app.services.storage import shot_output_path
from tests.integration.conftest import HEADERS, _make_project, _add_shot


def _make_tiny_mp4(path: Path) -> None:
    """生成一个 0.5s 的合法小 mp4（带音视频流），供 concat copy 使用。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=0.5",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


async def _add_shot_with_video(db_session_factory, project_id, shot_id):
    """新增一个 completed shot，并生成真实 fixture 视频、写入 video_path。"""
    await _add_shot(db_session_factory, project_id, shot_id, status="completed")
    out = Path(shot_output_path(project_id, shot_id))
    _make_tiny_mp4(out)
    async with db_session_factory() as s:
        from sqlalchemy import select
        from app.models.project import Shot
        row = (
            await s.execute(
                select(Shot).where(
                    Shot.project_id == project_id, Shot.shot_id == shot_id
                )
            )
        ).scalar_one()
        row.video_path = str(out)
        await s.commit()


@pytest.mark.asyncio
async def test_join_preview_success(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    for i in (1, 2, 3):
        await _add_shot_with_video(db_session_factory, pid, i)

    r = await client.post(
        f"/api/projects/{pid}/join-preview",
        json={"shot_ids": [2, 3]},
        headers=HEADERS,
    )

    assert r.status_code == 200, r.text
    url = r.json()["preview_url"]
    assert "/api/media/" in url and "join_preview.mp4" in url
    # 实际输出文件已生成
    out = Path(settings.storage_root) / "projects" / pid / "previews" / "join_preview.mp4"
    assert out.is_file() and out.stat().st_size > 0


@pytest.mark.asyncio
async def test_join_preview_requires_two_shots(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot_with_video(db_session_factory, pid, 1)

    r = await client.post(
        f"/api/projects/{pid}/join-preview",
        json={"shot_ids": [1]},
        headers=HEADERS,
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_join_preview_rejects_incomplete_shot(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot_with_video(db_session_factory, pid, 1)
    # shot 2 是 pending、无 video_path
    await _add_shot(db_session_factory, pid, 2, status="pending")

    r = await client.post(
        f"/api/projects/{pid}/join-preview",
        json={"shot_ids": [1, 2]},
        headers=HEADERS,
    )
    assert r.status_code == 400
    assert "2" in r.json()["detail"]
```

> 注：`_make_project` 接受 `status` 参数；用 `"shot_review"` 是为了语义贴近真实场景，端点本身不校验 project 状态，所以任何 status 都不影响断言。若该 status 字符串不被接受，改用默认 `"draft"`。

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project backend pytest backend/tests/integration/test_join_preview.py -v`
Expected: FAIL —— 端点不存在，返回 404（`assert r.status_code == 200` 失败）。

- [ ] **Step 3: Write the schema**

In `backend/app/models/schemas.py`, after `ExportRequest` (around line 184), add:

```python
class JoinPreviewRequest(BaseModel):
    shot_ids: list[int]
```

- [ ] **Step 4: Wire imports + implement the endpoint**

In `backend/app/api/pipeline.py`:

(a) Add `JoinPreviewRequest` to the schemas import block (around line 22-26):

```python
from app.models.schemas import (
    ProjectResponse, StoryboardUpdate, ShotUpdate, ShotAiEditRequest,
    ShotTrimRequest, RegenerateShotsRequest, PipelineActionResponse,
    ReferenceVoiceRequest, ExportRequest, JoinPreviewRequest,
)
```

(b) Add `join_preview_path` to the storage import block (around line 31-35):

```python
from app.services.storage import (
    storyboard_path, archived_storyboard_path, shot_custom_frames_dir, to_media_url,
    shot_pre_vc_video_path, shot_audio_original_path, shot_audio_vc_path,
    shot_pre_cc_last_frame_path, join_preview_path,
)
```

(c) After the `export_project` endpoint (around line 687), add:

```python
@router.post("/projects/{project_id}/join-preview")
async def join_preview(
    project_id: str,
    body: JoinPreviewRequest,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """临时把选中的 shot 纯拼接成一条预览视频，用于检测连贯性。同步执行。"""
    from app.agents.merger import merge_shots

    await _get_project_or_404(project_id, session)

    if len(body.shot_ids) < 2:
        raise HTTPException(
            status_code=400, detail="至少选择 2 个镜头才能拼接预览"
        )

    result = await session.execute(
        select(Shot).where(
            Shot.project_id == project_id,
            Shot.shot_id.in_(body.shot_ids),
        )
    )
    shots_by_id = {s.shot_id: s for s in result.scalars().all()}

    ordered_paths: list[str] = []
    for sid in body.shot_ids:
        shot = shots_by_id.get(sid)
        if shot is None:
            raise HTTPException(status_code=400, detail=f"镜头 {sid} 不存在")
        if shot.status != ShotStatus.COMPLETED.value:
            raise HTTPException(
                status_code=400, detail=f"镜头 {sid} 尚未完成，无法预览"
            )
        if not shot.video_path or not Path(shot.video_path).exists():
            raise HTTPException(
                status_code=400, detail=f"镜头 {sid} 缺少视频文件"
            )
        ordered_paths.append(shot.video_path)

    output_path = str(join_preview_path(project_id))
    try:
        merge_shots(ordered_paths, output_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"拼接失败: {e}")

    media_url = to_media_url(output_path)
    # cache-busting：用输出文件大小，避免浏览器/video 缓存旧预览
    bust = Path(output_path).stat().st_size
    return {"preview_url": f"{media_url}?t={bust}"}
```

> `Path` 已在 pipeline.py 顶部 import（`from pathlib import Path`，line 7）；`select`、`Shot`、`ShotStatus`、`HTTPException`、`Depends`、`AsyncSession`、`get_session`、`_require_user`、`_get_project_or_404` 均已存在。

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --project backend pytest backend/tests/integration/test_join_preview.py -v`
Expected: PASS（3 passed）。若环境无 `ffmpeg`，先确认 `ffmpeg -version` 可用。

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/schemas.py backend/app/api/pipeline.py backend/tests/integration/test_join_preview.py
git commit -m "feat(pipeline): add synchronous join-preview endpoint for continuity check"
```

---

### Task 3: 前端 API client + "连贯性预览" 按钮 + 播放 modal

**Files:**
- Modify: `frontend-vite/src/lib/api.ts`（在 `exportVideo` 之后，约 line 213）
- Modify: `frontend-vite/src/pages/ShotsPage.tsx`（批量操作区按钮 + 新增 state + handler + modal）
- Test: `frontend-vite/tests/join-preview.spec.ts`（Create）

**Interfaces:**
- Consumes:
  - 后端 `POST /api/projects/{id}/join-preview` body `{shot_ids: number[]}` → `{preview_url: string}`（Task 2）
  - 现有 `request<T>(method, path, body?)` helper（api.ts，自动带 `X-User-Name`）
  - 现有 ShotsPage 中的 `selectedShotIds: Set<number>`、`projectId`、`addToast`
- Produces:
  - `api.joinPreview(projectId: string, shotIds: number[]): Promise<{ preview_url: string }>`
  - ShotsPage 新按钮「连贯性预览」+ 播放预览的 modal

- [ ] **Step 1: Add API client method**

In `frontend-vite/src/lib/api.ts`, after `exportVideo` (around line 213), add:

```typescript
  // 临时拼接选中镜头，用于检测连贯性
  joinPreview: (
    id: string,
    shotIds: number[]
  ): Promise<{ preview_url: string }> => {
    return request('POST', `/api/projects/${id}/join-preview`, {
      shot_ids: shotIds,
    })
  },
```

- [ ] **Step 2: Add state + handler in ShotsPage**

In `frontend-vite/src/pages/ShotsPage.tsx`, near the other `useState` hooks (top of component), add:

```typescript
  const [joinPreviewUrl, setJoinPreviewUrl] = useState<string | null>(null)
  const [isJoining, setIsJoining] = useState(false)
```

Then add a handler alongside `handleRegenerate` (around line 173):

```typescript
  const handleJoinPreview = async () => {
    if (!projectId || selectedShotIds.size < 2) return
    setIsJoining(true)
    try {
      const ids = Array.from(selectedShotIds).sort((a, b) => a - b)
      const { preview_url } = await api.joinPreview(projectId, ids)
      setJoinPreviewUrl(preview_url)
    } catch (e) {
      addToast({
        type: 'error',
        message: e instanceof Error ? e.message : '拼接预览失败',
      })
    } finally {
      setIsJoining(false)
    }
  }
```

> 确认组件作用域内已有 `projectId`、`selectedShotIds`、`addToast`、`api`、`useState`（其它 handler 已在用，应已 import）。若 `Loader2` 未 import，从 `lucide-react` 引入（文件已 import 该库）。

- [ ] **Step 3: Add the button in the batch action row**

In `frontend-vite/src/pages/ShotsPage.tsx`, inside the `<div className="flex gap-3">` batch-action row (around line 805, next to 「重跑选中的镜」), add:

```tsx
                <Button
                  variant="outline"
                  data-testid="join-preview-button"
                  onClick={handleJoinPreview}
                  disabled={selectedShotIds.size < 2 || isJoining}
                >
                  {isJoining ? (
                    <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                  ) : (
                    <Film className="w-4 h-4 mr-2" />
                  )}
                  连贯性预览
                </Button>
```

> `Film` 图标从 `lucide-react` import（在文件顶部已有的 lucide import 列表里加上 `Film`）。若偏好已 import 的图标，可用 `RefreshCw` 之外的任意现有图标，但 `Film` 语义最贴切。

- [ ] **Step 4: Add the preview modal**

In `frontend-vite/src/pages/ShotsPage.tsx`, near the end of the returned JSX (before the closing fragment/root element), add a lightweight modal:

```tsx
      {joinPreviewUrl && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70"
          onClick={() => setJoinPreviewUrl(null)}
          data-testid="join-preview-modal"
        >
          <div
            className="relative bg-zinc-900 rounded-lg p-4 max-w-3xl w-full mx-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between mb-3">
              <span className="text-sm text-zinc-300">连贯性预览（临时拼接）</span>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setJoinPreviewUrl(null)}
              >
                关闭
              </Button>
            </div>
            <video
              src={joinPreviewUrl}
              controls
              autoPlay
              className="w-full rounded"
            />
          </div>
        </div>
      )}
```

> 关闭时把 `joinPreviewUrl` 设为 `null`，组件卸载 `<video>`，自动停止播放。

- [ ] **Step 5: Write the Playwright test (mock the endpoint)**

Create `frontend-vite/tests/join-preview.spec.ts`. 复用 `projects.spec.ts` 的导航/选择模式（先查看该文件确认进入 shot review 页、选中 shot 的现有写法，并照搬其 setup 与 mock 风格）。核心断言：

```typescript
import { test, expect } from '@playwright/test'

// 复用 projects.spec.ts 的项目创建/进入 shot review 的 helper 或步骤。
// 关键：mock join-preview 端点，避免触发真实 ffmpeg。
test('连贯性预览：选中 2 个镜头后弹出播放 modal', async ({ page }) => {
  await page.route('**/api/projects/*/join-preview', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ preview_url: '/api/media/x/previews/join_preview.mp4?t=1' }),
    })
  })

  // TODO(执行者)：照搬 projects.spec.ts 把一个项目带到 shot review 状态、
  // 渲染出至少 2 个 completed shot，并勾选其中 2 个。

  // 选中 <2 时按钮禁用
  const btn = page.getByTestId('join-preview-button')
  // 勾选 2 个 shot 后：
  await btn.click()
  await expect(page.getByTestId('join-preview-modal')).toBeVisible()
  await expect(page.locator('[data-testid="join-preview-modal"] video')).toHaveAttribute(
    'src',
    /join_preview\.mp4/,
  )
})
```

> 执行者必须先读 `frontend-vite/tests/projects.spec.ts`，按其既有方式把页面带到「批量操作区可见、有 ≥2 个 completed shot 可勾选」的状态，替换上面的 TODO。其余 AI 触发端点按 CLAUDE.md 一并 mock。

- [ ] **Step 6: Run the Playwright test**

Run: `cd frontend-vite && npx playwright test join-preview.spec.ts`
Expected: PASS（modal 出现、video src 正确；按钮禁用条件正确）。

- [ ] **Step 7: Commit**

```bash
git add frontend-vite/src/lib/api.ts frontend-vite/src/pages/ShotsPage.tsx frontend-vite/tests/join-preview.spec.ts
git commit -m "feat(shots): add continuity join-preview button and player modal"
```

---

## 完整验证（实现完成后）

- [ ] 后端全量：`uv run --project backend pytest backend/tests/unit/test_storage_join_preview.py backend/tests/integration/test_join_preview.py -v` 全绿。
- [ ] 前端：`cd frontend-vite && npx playwright test join-preview.spec.ts` 全绿。
- [ ] 手动冒烟（podman 起 dev stack + vite）：进入某 project 的 shot review，勾选 2+ 相邻 completed shot → 点「连贯性预览」→ modal 中拼接视频可连续播放；关闭后再次预览其它组合，确认无旧缓存（URL `?t=` 变化）。
- [ ] 确认 `final/merged.mp4` 未被改动（本功能只写 `previews/join_preview.mp4`）。

## Self-Review 记录

- **Spec coverage**：storage helper（Task 1）、同步端点+校验+合并+cache-busting URL（Task 2）、API client+按钮+modal（Task 3）、不触发素材审计（Global Constraints + 验证项）——spec 各节均有对应任务。
- **Placeholder scan**：仅 Playwright 测试有一处 `TODO(执行者)`，因导航/选中步骤须照搬现有 `projects.spec.ts`，无法在不读该文件时给出确定代码——已明确指示执行者读取并替换，非笼统占位。
- **Type consistency**：`join_preview_path`、`merge_shots(paths, output_path)`、`JoinPreviewRequest.shot_ids`、`api.joinPreview(id, shotIds)`、`{preview_url}`、`join-preview-button` / `join-preview-modal` testid 在各任务间一致。
