# 视频模型切换功能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户按镜头选择视频生成模型（`veo` 默认 / `seeddance2.0`），项目级可设默认、镜头级可覆盖；厂商自动决定，默认 `kie`。

**Architecture:** 沿用现有 `VideoProvider` ABC。抽出 `_KieBase` 共享 kie 上传/鉴权逻辑，新增 `KieSeedanceProvider` 走 kie 通用 jobs API（`/api/v1/jobs/createTask` + `recordInfo` 轮询）。`get_video_provider(model)` 解析：`seeddance2.0`→恒 kie seedance；`veo`→按全局 `settings.video_provider`（默认翻成 `kie`，保留 vertex 退路）。镜头模型存 DB（`Shot.video_model`），新镜头继承 `Project.default_video_model`，worker 生成时按镜头模型路由。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy(async, SQLite) / httpx / pydantic-settings；前端 React + Vite + TypeScript。

## Global Constraints

- Python 包管理用 `pyproject.toml`，运行脚本/测试用 `uv`（**禁** `python`/`pip`）。后端测试命令：`uv run --project backend pytest`。
- 不硬编码绝对路径；不硬编码任何密钥（kie 复用现有 `kie_api_key`，无新 secret）。
- 所有 AI/模型调用在测试中必须 mock（httpx 全 mock），不产生计费。
- 模型枚举值固定为字符串 `"veo"` 与 `"seeddance2.0"`（注意 seedance 值含点号）。
- seedance kie model id 默认 `"bytedance/seedance-2-fast"`，可经 `settings.kie_seedance_model` 配置。
- seedance `duration` 合法区间 4–15s；`generate_audio` 默认 `True`。
- 输出文件仍写 `output.mp4`，不改素材文件命名/备份逻辑。

---

### Task 1: DB 列 + 幂等迁移 + 模型枚举

**Files:**
- Modify: `backend/app/models/project.py`
- Modify: `backend/app/db.py`（`_run_migrations()`）
- Test: `backend/tests/unit/test_video_model_schema.py`（Create）

**Interfaces:**
- Produces: `VideoModel` 枚举（`VideoModel.VEO == "veo"`, `VideoModel.SEEDDANCE_2 == "seeddance2.0"`）；`Project.default_video_model: str`（默认 `"veo"`）；`Shot.video_model: str`（默认 `"veo"`）。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/unit/test_video_model_schema.py`:
```python
"""Schema-level tests for per-shot video model selection."""
from app.models.project import Project, Shot, VideoModel


def test_video_model_enum_values():
    assert VideoModel.VEO.value == "veo"
    assert VideoModel.SEEDDANCE_2.value == "seeddance2.0"


def test_project_default_video_model_defaults_to_veo():
    p = Project(title="t", theme_text="x", creator_name="me")
    # column default applies on flush; verify the declared default value
    assert Project.__table__.c.default_video_model.default.arg == "veo"


def test_shot_video_model_defaults_to_veo():
    assert Shot.__table__.c.video_model.default.arg == "veo"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest tests/unit/test_video_model_schema.py -v`
Expected: FAIL — `ImportError: cannot import name 'VideoModel'`

- [ ] **Step 3: 加枚举 + 列**

In `backend/app/models/project.py`, 在 `ShotStatus` 枚举附近新增：
```python
class VideoModel(str, Enum):
    VEO = "veo"
    SEEDDANCE_2 = "seeddance2.0"
```

`Project` 类新增列（放在 `aspect_ratio` 之后）：
```python
    default_video_model = Column(String(20), nullable=False, default=VideoModel.VEO.value)
```

`Shot` 类新增列（放在 `shot_duration` 之后）：
```python
    video_model = Column(String(20), nullable=False, default=VideoModel.VEO.value)
```

- [ ] **Step 4: 加幂等迁移**

In `backend/app/db.py` 的 `_run_migrations(conn)` 末尾追加：
```python
    if not await _has_column("projects", "default_video_model"):
        await conn.execute(sa.text(
            "ALTER TABLE projects ADD COLUMN default_video_model VARCHAR(20) NOT NULL DEFAULT 'veo'"
        ))
    if not await _has_column("shots", "video_model"):
        await conn.execute(sa.text(
            "ALTER TABLE shots ADD COLUMN video_model VARCHAR(20) NOT NULL DEFAULT 'veo'"
        ))
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run --project backend pytest tests/unit/test_video_model_schema.py -v`
Expected: PASS（3 passed）

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/project.py backend/app/db.py backend/tests/unit/test_video_model_schema.py
git commit -m "feat(db): add video_model columns + migration for model switching"
```

---

### Task 2: KieSeedanceProvider（含 _KieBase 重构 + seedance 配置）

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/agents/video_generator.py`
- Test: `backend/tests/unit/test_seedance_provider.py`（Create）

**Interfaces:**
- Consumes: 现有 `VideoProvider` ABC、`_crop_inputs`、`_build_prompt`、`VideoGenerationError`、`VideoGenerationTimeout`、`settings`。
- Produces:
  - `_KieBase(VideoProvider)`，含 `_headers()`、`_upload_image(http, path)`（从 `KieVeoProvider` 上移）。
  - `KieVeoProvider(_KieBase)`（行为不变）。
  - `KieSeedanceProvider(_KieBase)`，`generate_video(...)` 同签名；常量 `CREATE_PATH="/api/v1/jobs/createTask"`、`RECORD_PATH="/api/v1/jobs/recordInfo"`；静态方法 `_resolve_inputs(first, last, refs) -> tuple[str, list[str]]`（scenario ∈ `"reference"|"frames"|"text"`）。
  - `_clamp_seedance_duration(seconds:int)->int`（夹到 4–15）。
  - `settings.kie_seedance_model: str`、`settings.kie_seedance_generate_audio: bool`。

- [ ] **Step 1: 加 seedance 配置**

In `backend/app/config.py`，kie 配置区追加：
```python
    # seedance via kie.ai generic jobs API (used when video_model == "seeddance2.0")
    kie_seedance_model: str = "bytedance/seedance-2-fast"
    kie_seedance_generate_audio: bool = True
```

- [ ] **Step 2: 写失败测试**

Create `backend/tests/unit/test_seedance_provider.py`:
```python
"""Unit tests for the kie.ai seedance video provider (httpx mocked)."""
import json
import pytest
from unittest.mock import patch

import app.agents.video_generator as vg
from app.agents.video_generator import (
    KieSeedanceProvider,
    VideoGenerationError,
    _clamp_seedance_duration,
)


def test_clamp_seedance_duration():
    assert _clamp_seedance_duration(3) == 4
    assert _clamp_seedance_duration(8) == 8
    assert _clamp_seedance_duration(15) == 15
    assert _clamp_seedance_duration(20) == 15


def test_resolve_inputs_priority():
    assert KieSeedanceProvider._resolve_inputs(None, None, None) == ("text", [])
    assert KieSeedanceProvider._resolve_inputs("/a.png", None, None) == ("frames", ["/a.png"])
    assert KieSeedanceProvider._resolve_inputs("/a.png", "/b.png", None) == ("frames", ["/a.png", "/b.png"])
    # reference wins over frames and caps at 9
    scenario, imgs = KieSeedanceProvider._resolve_inputs("/a.png", "/b.png", [f"/r{i}.png" for i in range(10)])
    assert scenario == "reference"
    assert len(imgs) == 9


class _FakeResponse:
    def __init__(self, json_data=None, content=b""):
        self._json = json_data
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class _FakeSeedanceClient:
    """Routes seedance jobs endpoints to canned responses; records the calls."""

    def __init__(self, record, state="success", **kwargs):
        self.record = record
        self.state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        if "file-base64-upload" in url:
            self.record["uploads"].append(json)
            n = len(self.record["uploads"])
            return _FakeResponse({"success": True, "data": {"downloadUrl": f"https://h/up{n}.png"}})
        if "/jobs/createTask" in url:
            self.record["create"] = json
            return _FakeResponse({"code": 200, "msg": "success", "data": {"taskId": "task_seed_1"}})
        raise AssertionError(f"unexpected POST {url}")

    async def get(self, url, params=None):
        if "/jobs/recordInfo" in url:
            self.record["polled"] = params
            if self.state == "fail":
                return _FakeResponse({"data": {"state": "fail", "failMsg": "boom", "failCode": "E1"}})
            return _FakeResponse({"data": {
                "state": "success",
                "resultJson": json.dumps({"resultUrls": ["https://h/seed.mp4"]}),
            }})
        if url == "https://h/seed.mp4":
            return _FakeResponse(content=b"SEEDMP4")
        raise AssertionError(f"unexpected GET {url}")


@pytest.fixture
def seed_env(monkeypatch):
    monkeypatch.setattr(vg.settings, "kie_api_key", "test-key")
    monkeypatch.setattr(vg.settings, "kie_poll_interval_seconds", 0)
    monkeypatch.setattr(vg.settings, "kie_seedance_model", "bytedance/seedance-2-fast")
    monkeypatch.setattr(vg.settings, "kie_seedance_generate_audio", True)
    monkeypatch.setattr(vg, "center_crop_to_aspect", lambda p, *a, **k: p)


def _patch_httpx(record, state="success"):
    def factory(*args, **kwargs):
        return _FakeSeedanceClient(record, state=state, **kwargs)
    return patch("app.agents.video_generator.httpx.AsyncClient", side_effect=factory)


@pytest.mark.asyncio
async def test_seedance_create_task_payload(seed_env, tmp_path):
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"\x89PNG fake")
    record = {"uploads": []}

    with _patch_httpx(record):
        out = await KieSeedanceProvider().generate_video(
            motion_prompt="A cat walks forward",
            first_frame_path=str(frame),
            shot_duration=6,
            spoken_text="Meow",
            aspect_ratio="16:9",
        )

    assert out == b"SEEDMP4"
    create = record["create"]
    assert create["model"] == "bytedance/seedance-2-fast"
    inp = create["input"]
    assert inp["first_frame_url"] == "https://h/up1.png"
    assert "last_frame_url" not in inp
    assert inp["duration"] == 6
    assert inp["aspect_ratio"] == "16:9"
    assert inp["generate_audio"] is True
    assert "Meow" in inp["prompt"]
    assert record["polled"] == {"taskId": "task_seed_1"}


@pytest.mark.asyncio
async def test_seedance_reference_inputs_only(seed_env, tmp_path):
    refs = []
    for i in range(2):
        p = tmp_path / f"r{i}.png"
        p.write_bytes(b"\x89PNG")
        refs.append(str(p))
    frame = tmp_path / "f.png"
    frame.write_bytes(b"\x89PNG")
    record = {"uploads": []}

    with _patch_httpx(record):
        await KieSeedanceProvider().generate_video(
            motion_prompt="ref shot",
            first_frame_path=str(frame),
            shot_duration=8,
            spoken_text="",
            reference_image_paths=refs,
        )

    inp = record["create"]["input"]
    # mutually exclusive: reference images present -> no frame URLs
    assert "reference_image_urls" in inp
    assert "first_frame_url" not in inp
    assert "last_frame_url" not in inp


@pytest.mark.asyncio
async def test_seedance_poll_fail_raises(seed_env, tmp_path):
    record = {"uploads": []}
    with _patch_httpx(record, state="fail"):
        with pytest.raises(VideoGenerationError) as exc:
            await KieSeedanceProvider().generate_video(
                motion_prompt="t", first_frame_path=None,
                shot_duration=5, spoken_text="",
            )
    assert "boom" in str(exc.value)
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run --project backend pytest tests/unit/test_seedance_provider.py -v`
Expected: FAIL — `ImportError: cannot import name 'KieSeedanceProvider'`

- [ ] **Step 4: 抽 `_KieBase` 并改 `KieVeoProvider` 继承它**

In `backend/app/agents/video_generator.py`，在 `KieVeoProvider` 之前新增基类，并把 `_headers` / `_upload_image` 从 `KieVeoProvider` 移到基类：
```python
class _KieBase(VideoProvider):
    """Shared kie.ai helpers: auth headers + base64 image upload."""

    UPLOAD_PATH = "/api/file-base64-upload"

    def _headers(self) -> dict:
        if not settings.kie_api_key:
            raise VideoGenerationError(
                "kie.ai API key not configured (set secrets/kie_api_key)"
            )
        return {
            "Authorization": f"Bearer {settings.kie_api_key}",
            "Content-Type": "application/json",
        }

    async def _upload_image(self, http: httpx.AsyncClient, path: str) -> str:
        raw = Path(path).read_bytes()
        b64 = base64.b64encode(raw).decode("ascii")
        mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
        payload = {
            "base64Data": f"data:{mime};base64,{b64}",
            "uploadPath": "video-maker/frames",
            "fileName": Path(path).name,
        }
        resp = await http.post(f"{settings.kie_upload_url}{self.UPLOAD_PATH}", json=payload)
        resp.raise_for_status()
        body = resp.json()
        url = (body.get("data") or {}).get("downloadUrl")
        if not body.get("success") or not url:
            raise VideoGenerationError(f"kie.ai image upload failed: {body}")
        logger.info("Uploaded %s -> %s", Path(path).name, url)
        return url
```

改 `class KieVeoProvider(VideoProvider):` → `class KieVeoProvider(_KieBase):`，并删除其中已上移的 `UPLOAD_PATH`、`_headers`、`_upload_image`（保留 `GENERATE_PATH`、`RECORD_PATH`、`_resolve_mode`、`_create_task`、`_poll_result`、`generate_video`）。

- [ ] **Step 5: 新增 `KieSeedanceProvider`**

In `backend/app/agents/video_generator.py`，在 `KieVeoProvider` 之后新增：
```python
_SEEDANCE_MIN_DURATION = 4
_SEEDANCE_MAX_DURATION = 15


def _clamp_seedance_duration(seconds: int) -> int:
    """Snap an arbitrary shot duration into seedance's accepted 4-15s range."""
    return max(_SEEDANCE_MIN_DURATION, min(_SEEDANCE_MAX_DURATION, seconds))


class KieSeedanceProvider(_KieBase):
    """Seedance video generation via kie.ai generic jobs API.

    Flow: base64-upload local frames -> hosted URLs, POST /api/v1/jobs/createTask,
    poll GET /api/v1/jobs/recordInfo until ``state`` settles, then download MP4.
    """

    CREATE_PATH = "/api/v1/jobs/createTask"
    RECORD_PATH = "/api/v1/jobs/recordInfo"

    @staticmethod
    def _resolve_inputs(
        first_frame_path, last_frame_path, reference_image_paths,
    ) -> tuple[str, list[str]]:
        """Priority: reference (max 9) > first/last frame > text. Inputs are
        mutually exclusive per seedance's API."""
        if reference_image_paths:
            return "reference", reference_image_paths[:9]
        if first_frame_path:
            imgs = [first_frame_path]
            if last_frame_path:
                imgs.append(last_frame_path)
            return "frames", imgs
        return "text", []

    async def _create_task(self, http, prompt, scenario, image_urls, duration, aspect_ratio) -> str:
        input_payload: dict = {
            "prompt": prompt,
            "resolution": settings.kie_resolution,
            "aspect_ratio": aspect_ratio if aspect_ratio in ("16:9", "9:16") else "adaptive",
            "duration": duration,
            "generate_audio": settings.kie_seedance_generate_audio,
        }
        if scenario == "reference":
            input_payload["reference_image_urls"] = image_urls
        elif scenario == "frames":
            input_payload["first_frame_url"] = image_urls[0]
            if len(image_urls) > 1:
                input_payload["last_frame_url"] = image_urls[1]

        body = {"model": settings.kie_seedance_model, "input": input_payload}
        logger.info(
            "kie.ai seedance: model=%s, scenario=%s, images=%d, duration=%s, prompt=%s",
            settings.kie_seedance_model, scenario, len(image_urls), duration, prompt[:500],
        )
        resp = await http.post(f"{settings.kie_base_url}{self.CREATE_PATH}", json=body)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 200:
            raise VideoGenerationError(f"kie.ai seedance create rejected: {data}")
        task_id = (data.get("data") or {}).get("taskId")
        if not task_id:
            raise VideoGenerationError(f"kie.ai seedance returned no taskId: {data}")
        logger.info("kie.ai seedance task created: %s", task_id)
        return task_id

    async def _poll_result(self, http, task_id: str) -> str:
        elapsed = 0
        poll_interval = settings.kie_poll_interval_seconds
        max_wait = settings.kie_max_wait_seconds
        while True:
            resp = await http.get(
                f"{settings.kie_base_url}{self.RECORD_PATH}", params={"taskId": task_id},
            )
            resp.raise_for_status()
            data = resp.json().get("data") or {}
            state = data.get("state")
            if state == "success":
                result_json = data.get("resultJson")
                parsed = json.loads(result_json) if isinstance(result_json, str) else (result_json or {})
                urls = parsed.get("resultUrls")
                if not urls:
                    raise VideoGenerationError(
                        f"kie.ai seedance task {task_id} succeeded but returned no URL: {data}"
                    )
                return urls[0]
            if state == "fail":
                raise VideoGenerationError(
                    f"kie.ai seedance task {task_id} failed "
                    f"(code={data.get('failCode')}): {data.get('failMsg')}"
                )
            if elapsed >= max_wait:
                raise VideoGenerationTimeout(
                    f"kie.ai seedance task {task_id} timed out after {max_wait}s"
                )
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            logger.debug("Polling seedance task %s... elapsed=%ds", task_id, elapsed)

    async def generate_video(
        self,
        motion_prompt: str,
        first_frame_path=None,
        shot_duration: int = 8,
        spoken_text: str = "",
        reference_image_paths=None,
        aspect_ratio: str = "16:9",
        last_frame_path=None,
    ) -> bytes:
        import tempfile, shutil
        tmp_dir = tempfile.mkdtemp(prefix="seed_crop_")
        api_timeout = 120
        try:
            first_frame_path, last_frame_path, reference_image_paths = _crop_inputs(
                tmp_dir, first_frame_path, last_frame_path,
                reference_image_paths, aspect_ratio,
            )
            prompt = _build_prompt(motion_prompt, spoken_text)
            scenario, image_paths = self._resolve_inputs(
                first_frame_path, last_frame_path, reference_image_paths,
            )
            duration = _clamp_seedance_duration(shot_duration)

            timeout = httpx.Timeout(api_timeout)
            async with httpx.AsyncClient(headers=self._headers(), timeout=timeout) as http:
                image_urls = [await self._upload_image(http, p) for p in image_paths]
                task_id = await self._create_task(
                    http, prompt, scenario, image_urls, duration, aspect_ratio,
                )
                result_url = await self._poll_result(http, task_id)

                logger.info("Downloading seedance result: %s", result_url)
                async with httpx.AsyncClient(timeout=timeout) as dl:
                    video_resp = await dl.get(result_url)
                    video_resp.raise_for_status()
                    video_bytes = video_resp.content

            logger.info("Video generated successfully (seedance): %d bytes", len(video_bytes))
            return video_bytes
        except (VideoGenerationTimeout, VideoGenerationError):
            raise
        except httpx.HTTPError as e:
            raise VideoGenerationError(f"kie.ai seedance HTTP error: {e}")
        except Exception as e:
            raise VideoGenerationError(f"Unexpected error during seedance generation: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
```

> 注：`_crop_inputs`、`_build_prompt`、`tempfile`/`shutil` 已在文件中被 `KieVeoProvider` 使用（`shutil`/`tempfile` 已在模块顶部 import 则去掉局部 import）。检查模块顶部 import，若已存在则删除 `generate_video` 内的 `import tempfile, shutil`。

- [ ] **Step 6: 跑 seedance + 既有测试确认通过**

Run: `uv run --project backend pytest tests/unit/test_seedance_provider.py tests/unit/test_video_generator.py -v`
Expected: PASS（seedance 全过；既有 kie/vertex 测试不回归）

- [ ] **Step 7: Commit**

```bash
git add backend/app/config.py backend/app/agents/video_generator.py backend/tests/unit/test_seedance_provider.py
git commit -m "feat(video): add KieSeedanceProvider via kie.ai jobs API"
```

---

### Task 3: 厂商解析器 + generate_video model 参数 + 翻默认 provider

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/agents/video_generator.py`
- Modify: `backend/tests/unit/test_video_generator.py`

**Interfaces:**
- Consumes: Task 2 的 `KieSeedanceProvider`。
- Produces: `get_video_provider(model: str = "veo") -> VideoProvider`；`generate_video(..., model: str = "veo")`。

- [ ] **Step 1: 写/改失败测试**

In `backend/tests/unit/test_video_generator.py`，新增解析器测试，并更新默认 provider 测试：
```python
def test_video_provider_config_default_is_kie():
    from app.config import Settings
    assert Settings().video_provider == "kie"


def test_resolve_seedance_always_kie(monkeypatch):
    from app.agents.video_generator import KieSeedanceProvider
    monkeypatch.setattr(vg.settings, "video_provider", "vertex")
    assert isinstance(get_video_provider("seeddance2.0"), KieSeedanceProvider)


def test_resolve_veo_follows_setting(monkeypatch):
    monkeypatch.setattr(vg.settings, "video_provider", "kie")
    assert isinstance(get_video_provider("veo"), KieVeoProvider)
    monkeypatch.setattr(vg.settings, "video_provider", "vertex")
    assert isinstance(get_video_provider("veo"), VertexVeoProvider)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest tests/unit/test_video_generator.py -k "resolve or config_default" -v`
Expected: FAIL — `test_video_provider_config_default_is_kie`（当前默认 vertex）；`get_video_provider("seeddance2.0")` 报多参/类型错误

- [ ] **Step 3: 翻默认 + 改解析器签名**

In `backend/app/config.py`：
```python
    video_provider: str = "kie"
```

In `backend/app/agents/video_generator.py`，替换 `_PROVIDERS` / `get_video_provider`：
```python
_VEO_PROVIDERS: dict[str, type[VideoProvider]] = {
    "vertex": VertexVeoProvider,
    "kie": KieVeoProvider,
}


def get_video_provider(model: str = "veo") -> VideoProvider:
    """Resolve a provider for the requested model.

    seeddance2.0 is kie-only; veo follows the global ``settings.video_provider``.
    """
    if model == "seeddance2.0":
        return KieSeedanceProvider()
    provider_cls = _VEO_PROVIDERS.get(settings.video_provider)
    if provider_cls is None:
        raise VideoGenerationError(
            f"Unknown video_provider '{settings.video_provider}' "
            f"(expected one of {sorted(_VEO_PROVIDERS)})"
        )
    return provider_cls()
```

改 `generate_video` 增加 `model` 形参并透传：
```python
async def generate_video(
    client=None,
    motion_prompt: str = "",
    first_frame_path=None,
    shot_duration: int = 8,
    spoken_text: str = "",
    operation_id=None,
    reference_image_paths=None,
    aspect_ratio: str = "16:9",
    last_frame_path=None,
    model: str = "veo",
) -> bytes:
    """Generate video via the provider resolved for ``model``."""
    provider = get_video_provider(model)
    return await provider.generate_video(
        motion_prompt=motion_prompt,
        first_frame_path=first_frame_path,
        shot_duration=shot_duration,
        spoken_text=spoken_text,
        reference_image_paths=reference_image_paths,
        aspect_ratio=aspect_ratio,
        last_frame_path=last_frame_path,
    )
```

> 既有 `test_kie_provider_selected` / `test_default_provider_is_vertex` 调用 `get_video_provider()`（无参，默认 `"veo"`）+ monkeypatch 设 `video_provider`，仍成立，无需改。

- [ ] **Step 4: 跑全套 video 测试确认通过**

Run: `uv run --project backend pytest tests/unit/test_video_generator.py tests/unit/test_seedance_provider.py -v`
Expected: PASS（含新解析器测试与配置默认测试）

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/app/agents/video_generator.py backend/tests/unit/test_video_generator.py
git commit -m "feat(video): route by model + default provider to kie"
```

---

### Task 4: API — 项目默认 + 镜头覆盖 + 响应字段 + 继承

**Files:**
- Modify: `backend/app/models/schemas.py`
- Modify: `backend/app/api/projects.py`
- Test: `backend/tests/unit/test_video_model_api.py`（Create）

**Interfaces:**
- Consumes: Task 1 列。
- Produces: `ProjectCreate.default_video_model`；`ShotUpdate.video_model`；`ProjectResponse.default_video_model`、`ShotResponse.video_model`；建 shot 时继承 `project.default_video_model`。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/unit/test_video_model_api.py`:
```python
"""API-level tests for video model selection."""
import pytest
from app.models.schemas import ProjectCreate, ShotUpdate, ProjectResponse, ShotResponse


def test_project_create_defaults_video_model_veo():
    body = ProjectCreate(title="t", theme_text="x")
    assert body.default_video_model == "veo"


def test_project_create_accepts_seedance():
    body = ProjectCreate(title="t", theme_text="x", default_video_model="seeddance2.0")
    assert body.default_video_model == "seeddance2.0"


def test_project_create_rejects_unknown_model():
    with pytest.raises(Exception):
        ProjectCreate(title="t", theme_text="x", default_video_model="sora")


def test_shot_update_allows_video_model():
    upd = ShotUpdate(video_model="seeddance2.0")
    assert upd.video_model == "seeddance2.0"


def test_responses_expose_video_model():
    assert "video_model" in ShotResponse.model_fields
    assert "default_video_model" in ProjectResponse.model_fields
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest tests/unit/test_video_model_api.py -v`
Expected: FAIL — `ProjectCreate` 无 `default_video_model` 字段

- [ ] **Step 3: 改 schemas**

In `backend/app/models/schemas.py`：
- `ShotResponse` 加（`shot_duration` 后）：`video_model: str = "veo"`
- `ProjectResponse` 加（`aspect_ratio` 后）：`default_video_model: str = "veo"`
- `ShotUpdate`（镜头更新 schema）加：`video_model: Optional[str] = Field(default=None, pattern="^(veo|seeddance2\\.0)$")`
- `ProjectCreate` 加：`default_video_model: str = Field(default="veo", pattern="^(veo|seeddance2\\.0)$")`

> 若 `ShotUpdate` 不存在，定位现有镜头更新端点用的请求模型，按上面字段加入。

- [ ] **Step 4: 建 project / shot 时写入与继承**

In `backend/app/api/projects.py` 的 `create_project`，构造 `Project(...)` 时加：
```python
        default_video_model=body.default_video_model,
```

定位生成分镜、批量创建 `Shot(...)` 的位置（脚本批准后建 shots），每个 `Shot(...)` 加：
```python
        video_model=project.default_video_model,
```
镜头更新端点：把请求体的 `video_model` 落到 `shot.video_model`（仅当非 None）。

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run --project backend pytest tests/unit/test_video_model_api.py -v`
Expected: PASS（5 passed）

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/schemas.py backend/app/api/projects.py backend/tests/unit/test_video_model_api.py
git commit -m "feat(api): expose default_video_model + per-shot video_model"
```

---

### Task 5: Worker 路由按镜头模型

**Files:**
- Modify: `backend/worker/tasks.py`
- Test: `backend/tests/unit/test_worker_video_model.py`（Create）

**Interfaces:**
- Consumes: Task 1 `shot.video_model`、Task 3 `generate_video(..., model=...)`。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/unit/test_worker_video_model.py`:
```python
"""Worker passes the shot's video_model into generate_video."""
import pytest
from unittest.mock import AsyncMock, patch
import worker.tasks as wt


@pytest.mark.asyncio
async def test_generate_video_called_with_shot_model(monkeypatch):
    captured = {}

    async def fake_generate_video(*args, **kwargs):
        captured["model"] = kwargs.get("model")
        return b"MP4"

    monkeypatch.setattr(wt, "generate_video", fake_generate_video)
    # The pipeline computes: model = shot.video_model or project.default_video_model
    model = wt._resolve_shot_model(  # helper added in Step 3
        shot_video_model="seeddance2.0", project_default="veo",
    )
    assert model == "seeddance2.0"
    assert wt._resolve_shot_model(shot_video_model=None, project_default="veo") == "veo"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest tests/unit/test_worker_video_model.py -v`
Expected: FAIL — `worker.tasks` 无 `_resolve_shot_model`

- [ ] **Step 3: 加 helper 并在调用点传 model**

In `backend/worker/tasks.py`，模块级新增：
```python
def _resolve_shot_model(shot_video_model, project_default) -> str:
    """Per-shot model wins; fall back to project default, then 'veo'."""
    return shot_video_model or project_default or "veo"
```

改 `run_shot_pipeline` 内 `generate_video(...)` 调用（~344 行），加：
```python
            model=_resolve_shot_model(shot.video_model, project.default_video_model),
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --project backend pytest tests/unit/test_worker_video_model.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/worker/tasks.py backend/tests/unit/test_worker_video_model.py
git commit -m "feat(worker): route video generation by per-shot model"
```

---

### Task 6: 前端 — 项目默认按钮 + 镜头模型下拉

**Files:**
- Modify: `frontend-vite/src/lib/types.ts`
- Modify: `frontend-vite/src/pages/NewProjectPage.tsx`
- Modify: `frontend-vite/src/components/ShotCard.tsx`

**Interfaces:**
- Consumes: Task 4 API 字段（`default_video_model` 在 create body；`video_model` 在 shot update body）。

- [ ] **Step 1: 加类型**

In `frontend-vite/src/lib/types.ts`：
- `Shot` 接口加 `video_model: string`
- `Project` 接口加 `default_video_model: string`

- [ ] **Step 2: NewProjectPage 加默认模型按钮组**

In `frontend-vite/src/pages/NewProjectPage.tsx`，新增 state（镜像 `aspectRatio`）：
```tsx
  const [defaultVideoModel, setDefaultVideoModel] = useState<'veo' | 'seeddance2.0'>('veo')
```
在画面比例 `</div>` 之后新增按钮组：
```tsx
  {/* 视频模型 */}
  <div className="space-y-2">
    <Label>视频模型</Label>
    <div className="flex gap-3">
      {(['veo', 'seeddance2.0'] as const).map((m) => (
        <button
          key={m}
          type="button"
          data-testid={`video-model-${m}`}
          onClick={() => setDefaultVideoModel(m)}
          className={`px-4 py-2 rounded-lg border text-sm font-medium transition-colors ${
            defaultVideoModel === m
              ? 'border-blue-500 bg-blue-50 text-blue-700'
              : 'border-zinc-200 text-zinc-600 hover:border-zinc-300'
          }`}
        >
          {m === 'veo' ? 'Veo' : 'Seedance 2.0'}
        </button>
      ))}
    </div>
  </div>
```
在创建项目的请求体里带上 `default_video_model: defaultVideoModel`。

- [ ] **Step 3: ShotCard 编辑弹窗加模型下拉**

In `frontend-vite/src/components/ShotCard.tsx` 的编辑弹窗（时长选择器旁），加一个受控 `<select>`，初值 `shot.video_model`，onChange 写入编辑态；保存时把 `video_model` 一并 PATCH 到 shot 更新端点：
```tsx
  <div className="space-y-1">
    <Label>视频模型</Label>
    <select
      data-testid={`shot-video-model-${shot.shot_id}`}
      value={editVideoModel}
      onChange={(e) => setEditVideoModel(e.target.value)}
      className="w-full rounded-md border border-zinc-200 px-2 py-1 text-sm"
    >
      <option value="veo">Veo</option>
      <option value="seeddance2.0">Seedance 2.0</option>
    </select>
  </div>
```
（`editVideoModel` 用 `useState(shot.video_model ?? 'veo')` 初始化。）

- [ ] **Step 4: 前端构建确认无类型错误**

Run: `cd frontend-vite && npm run build`
Expected: 构建成功，无 TS 报错

- [ ] **Step 5: Commit**

```bash
git add frontend-vite/src/lib/types.ts frontend-vite/src/pages/NewProjectPage.tsx frontend-vite/src/components/ShotCard.tsx
git commit -m "feat(ui): project default + per-shot video model selector"
```

---

### Task 7: 端到端确认 + seedance 配音实测（Open Risk 验证）

**Files:** 无代码改动（除非实测发现问题）

- [ ] **Step 1: 跑后端全套测试**

Run: `uv run --project backend pytest tests/unit -q`
Expected: 全绿，无回归

- [ ] **Step 2: 起 dev 栈，建项目选 seedance，生成一个含台词镜头**

按 CLAUDE.md：`podman compose -f deploy/docker-compose.dev.yml up -d`（需 `KIE_API_KEY`）。前端建项目时默认模型选 Seedance 2.0，跑通一个含台词镜头的生成。

- [ ] **Step 3: 验证 Open Risk #1（seedance 配音）**

确认产出 `output.mp4` 的音轨**是否念出镜头台词**：
- 若念出 → 现有 VC 链路可用，无需改动。
- 若只出环境音/BGM → 记录问题；评估让 seedance 镜头改走纯 TTS 配音路径（不在本计划范围，另开任务）。

- [ ] **Step 4: 记录结论**

把实测结论补到 `docs/superpowers/specs/2026-06-17-video-model-switching-design.md` 的 Open Risks 段。

---

## Self-Review

**Spec coverage：**
- 模型枚举 + 项目默认 + 镜头列 → Task 1 ✓
- KieSeedanceProvider（createTask/recordInfo/duration clamp/互斥输入） → Task 2 ✓
- 方案 A 解析器 + 翻默认 kie + generate_video model 参数 → Task 3 ✓
- API（ProjectCreate/ShotUpdate/responses/继承） → Task 4 ✓
- Worker 路由 → Task 5 ✓
- 前端（types/NewProjectPage/ShotCard） → Task 6 ✓
- seedance 配音风险实测 → Task 7 ✓

**Placeholder scan：** 无 TBD/TODO；除 Task 4 中"若 ShotUpdate 不存在则定位现有镜头更新模型"与 Task 5 调用点行号为定位说明（实现时按现有代码确认），其余均给出可落地代码。

**Type consistency：** `get_video_provider(model)`、`generate_video(..., model=...)`、`_resolve_shot_model(...)`、`_resolve_inputs(...)`、`_clamp_seedance_duration(...)`、字段 `video_model` / `default_video_model` 跨任务命名一致。
