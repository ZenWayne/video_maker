# 非破坏式编辑（裁剪 + 配音）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把镜头的裁剪和配音从「物理改写视频文件」改成「只改 DB 元数据 + 播放/导出时合成」，源视频 `output_<ts>_<uuid>.mp4` 永不改动。

**Architecture:** EDL（Edit Decision List）模型——裁剪存 `trim_frames`、配音存 `vc_audio_path`（一条全长 wav）。两个消费方读同一份元数据：前端 `<ShotPlayer>` 实时合成预览（裁剪钳制 + video/audio 双元素同步），后端导出时 `build_effective_clip` 从源 + EDL 现烤。源视频字节级不可变。

**Tech Stack:** FastAPI + SQLAlchemy(async, sqlite+aiosqlite) + arq worker + python-ffmpeg；前端 React + Vite + TypeScript；测试 pytest（后端）/ vitest + Playwright（前端）。

## Global Constraints

- Python 一律 `uv run`，不直接 `python`/`pip`；测试 `uv run --project backend pytest`。
- 不硬编码绝对路径：Python 用 `Path(__file__)`，TS 用相对路径。
- 所有 AI/模型调用（CosyVoice voice_convert、Gemini 等）测试中必须 mock，禁止真实计费。
- Playwright 测试必须 mock AI 触发端点（`/start`、`/export`、`/voice-convert` 等），用真实 redis。
- 文件命名一律 `ts_uuid_name()`（`<unix>_<8hex>.<ext>`），**绝不原地覆盖**；被取代的旧文件显式删除。
- Google GenAI 必须 `vertexai=True`（本计划不新增 GenAI 调用，沿用现状）。
- 范围：仅 **裁剪 + 配音**。CC（角色校准）维持现状。不做向后兼容/迁移（全量重构，假设旧数据可重生）。
- 裁剪只支持「从头保留 N 帧」单值 `trim_frames`，不做区间裁剪。
- 关键不变量：源 `output_*.mp4` 在 `/trim`·`/restore-trim`·`/voice-convert`·`/voice-revert` 前后 md5 不变。

## File Structure

**后端**
- `backend/app/models/project.py` — Shot 新增 4 字段
- `backend/app/db.py` — `_run_migrations` 加 4 列 ALTER
- `backend/app/services/storage.py` — `shot_source_path()`；简化 `get_original_video_for_audio`
- `backend/app/agents/frame_porter.py` — 新增 `extract_frame_at()`
- `backend/app/agents/effective_clip.py`（**新**）— `build_effective_clip()` + `effective_clip_paths()`
- `backend/app/api/pipeline.py` — `/trim`、`/restore-trim` 改元数据；序列化辅助
- `backend/app/api/voice.py` — `/voice-revert` 改清元数据
- `backend/app/api/projects.py` — shot 序列化加播放描述字段
- `backend/worker/tasks.py` — 生成写 fps/frames；VC 只产 wav；`run_merger` 用 effective clips
- `backend/tests/test_nondestructive_*.py`（**新**）— 后端测试

**前端**
- `frontend-vite/src/hooks/useShotSync.ts`（**新**）— video/audio 同步
- `frontend-vite/src/components/ShotPlayer.tsx`（**新**）— 三模式播放器 + A/B 开关
- `frontend-vite/src/components/ShotCard.tsx` — 接入 `<ShotPlayer>`
- `frontend-vite/src/components/__tests__/ShotPlayer.test.tsx`（**新**）
- `tests/e2e/nondestructive-playback.spec.ts`（**新**，Playwright）

---

## Task 1: Shot 数据模型 + 迁移

**Files:**
- Modify: `backend/app/models/project.py:117-163`
- Modify: `backend/app/db.py:45-109`
- Test: `backend/tests/test_nondestructive_model.py`

**Interfaces:**
- Produces: Shot 新字段 `trim_frames: int|None`、`source_fps: float|None`、`source_frames: int|None`、`vc_audio_path: str|None`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_nondestructive_model.py
import pytest
from app.models.project import Shot


def test_shot_has_edl_columns():
    cols = {c.name for c in Shot.__table__.columns}
    assert {"trim_frames", "source_fps", "source_frames", "vc_audio_path"} <= cols
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest backend/tests/test_nondestructive_model.py -v`
Expected: FAIL（AssertionError，缺列）

- [ ] **Step 3: 加模型字段**

在 `backend/app/models/project.py` 的 Shot 类、`auto_trim` 行之后加：

```python
    # --- 非破坏式编辑 EDL ---
    trim_frames = Column(Integer, nullable=True)      # 从头保留帧数；None=不裁剪
    source_fps = Column(Float, nullable=True)         # 源视频 fps（生成时写入）
    source_frames = Column(Integer, nullable=True)    # 源视频总帧数
    vc_audio_path = Column(Text, nullable=True)       # 替换音轨 wav；None=用源原音
```

确认文件顶部 import 含 `Float`：`from sqlalchemy import (... Float ...)`，若无则加入。

- [ ] **Step 4: 加迁移**

在 `backend/app/db.py` `_run_migrations` 末尾（`auto_trim` 块之后）加：

```python
    for col, typ in [
        ("trim_frames", "INTEGER"),
        ("source_fps", "FLOAT"),
        ("source_frames", "INTEGER"),
        ("vc_audio_path", "TEXT"),
    ]:
        if not await _has_column("shots", col):
            await conn.execute(sa.text(f"ALTER TABLE shots ADD COLUMN {col} {typ}"))
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run --project backend pytest backend/tests/test_nondestructive_model.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/project.py backend/app/db.py backend/tests/test_nondestructive_model.py
git commit -m "feat(model): add EDL columns (trim_frames, source_fps/frames, vc_audio_path)"
```

---

## Task 2: 帧抽取助手 `extract_frame_at` + 源路径助手

**Files:**
- Modify: `backend/app/agents/frame_porter.py`
- Modify: `backend/app/services/storage.py:98-109`
- Test: `backend/tests/test_extract_frame_at.py`

**Interfaces:**
- Produces: `extract_frame_at(video_path: str, frame_index: int, output_path: str) -> None`（0-based，输出无损 PNG）
- Produces: `shot_source_path(project_id, shot_id) -> Path|None`（= 最新 `output_*.mp4`）
- Consumes: `app.agents.video_trimmer.get_video_info`（已存在，返回 `{fps,total_frames,duration}`）

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_extract_frame_at.py
import hashlib
import subprocess
from pathlib import Path

import pytest

from app.agents.frame_porter import extract_frame_at


@pytest.fixture
def color_video(tmp_path):
    """30 帧、每秒 30fps、每帧纯色按帧号渐变的无损测试视频。"""
    out = tmp_path / "src.mp4"
    # testsrc2 每帧内容不同（带帧号），ffv1 无损 → 帧字节确定
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=30",
         "-frames:v", "30", "-pix_fmt", "yuv420p", "-c:v", "ffv1", str(out)],
        check=True, capture_output=True,
    )
    return out


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def test_extract_frame_at_is_deterministic(color_video, tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    extract_frame_at(str(color_video), 9, str(a))
    extract_frame_at(str(color_video), 9, str(b))
    assert a.exists() and b.exists()
    assert _md5(a) == _md5(b)


def test_extract_frame_at_different_index_differs(color_video, tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    extract_frame_at(str(color_video), 5, str(a))
    extract_frame_at(str(color_video), 9, str(b))
    assert _md5(a) != _md5(b)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest backend/tests/test_extract_frame_at.py -v`
Expected: FAIL（ImportError: cannot import extract_frame_at）

- [ ] **Step 3: 实现 `extract_frame_at`**

在 `backend/app/agents/frame_porter.py` 末尾加：

```python
def extract_frame_at(video_path: str, frame_index: int, output_path: str) -> None:
    """Extract the single frame at 0-based *frame_index* as a lossless PNG.

    Used to refresh last_frame.png after a metadata-only trim: trimming to N
    frames keeps frames 0..N-1, so the new last frame is index N-1. PNG output
    is lossless, so md5 of the same (video, index) is byte-stable.
    """
    (
        FFmpeg()
        .option("y")
        .input(video_path)
        .output(
            output_path,
            vf=f"select='eq(n\\,{frame_index})'",
            vframes=1,
            vsync="0",
        )
    ).execute()
    if not Path(output_path).exists():
        raise RuntimeError(f"extract_frame_at: no frame {frame_index} in {video_path}")
```

- [ ] **Step 4: 实现 `shot_source_path` + 简化 `get_original_video_for_audio`**

在 `backend/app/services/storage.py` 加（紧接 `pristine_video_path` 之后）：

```python
def shot_source_path(project_id: str, shot_id: int) -> Optional[Path]:
    """The immutable source video (output_<ts>_<uuid>.mp4).

    In the non-destructive model this is the ONLY video file; trim/VC never
    write trimmed_/vc_ files. Alias of pristine_video_path for intent clarity.
    """
    return pristine_video_path(project_id, shot_id)
```

把 `get_original_video_for_audio` 函数体替换为（VC 输入 = 源整条音轨）：

```python
def get_original_video_for_audio(project_id: str, shot_id: int) -> Path:
    """Return the immutable source video to extract VC input audio from.

    Non-destructive model: there is exactly one video (output_*.mp4); VC reads
    its full audio and never depends on trim length.
    """
    src = shot_source_path(project_id, shot_id)
    if src is None:
        raise FileNotFoundError(f"No source video in {shot_dir(project_id, shot_id)}")
    return src
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run --project backend pytest backend/tests/test_extract_frame_at.py -v`
Expected: PASS（2 passed）

- [ ] **Step 6: Commit**

```bash
git add backend/app/agents/frame_porter.py backend/app/services/storage.py backend/tests/test_extract_frame_at.py
git commit -m "feat(storage): extract_frame_at + shot_source_path; VC audio reads full source"
```

---

## Task 3: 生成时记录 source_fps / source_frames

**Files:**
- Modify: `backend/worker/tasks.py:399-440`（生成块，写 `shot.video_path` 处）
- Test: `backend/tests/test_generation_records_source_meta.py`

**Interfaces:**
- Consumes: `app.agents.video_trimmer.get_video_info`
- Produces: 生成后 `shot.source_fps` / `shot.source_frames` 已填

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_generation_records_source_meta.py
import subprocess
from pathlib import Path

import pytest

from app.agents.video_trimmer import get_video_info


@pytest.fixture
def real_video(tmp_path):
    out = tmp_path / "v.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=30",
         "-frames:v", "48", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(out)],
        check=True, capture_output=True,
    )
    return out


def test_get_video_info_gives_fps_and_frames(real_video):
    info = get_video_info(str(real_video))
    assert round(info["fps"]) == 30
    assert info["total_frames"] == 48
```

> 说明：生成函数依赖真实 Veo 调用，不在单测内端到端跑；本测试锁住「我们用来填字段的 `get_video_info` 行为正确」，生成块只是把这两个值写进 DB（下方实现）。

- [ ] **Step 2: 跑测试确认通过（基线行为）**

Run: `uv run --project backend pytest backend/tests/test_generation_records_source_meta.py -v`
Expected: PASS（验证抽取 helper 正确）

- [ ] **Step 3: 在生成块写入字段**

在 `backend/worker/tasks.py` 生成成功、`shot.video_path = str(video_out)` 那一行（约 436）之后加：

```python
                from app.agents.video_trimmer import get_video_info as _gvi
                _src_info = _gvi(str(video_out))
                shot.source_fps = _src_info["fps"]
                shot.source_frames = _src_info["total_frames"]
                shot.trim_frames = None
                shot.vc_audio_path = None
```

- [ ] **Step 4: 静态校验 import 不重复 / 语法**

Run: `uv run --project backend python -c "import ast; ast.parse(open('backend/worker/tasks.py').read()); print('ok')"`
Expected: 打印 `ok`

- [ ] **Step 5: Commit**

```bash
git add backend/worker/tasks.py backend/tests/test_generation_records_source_meta.py
git commit -m "feat(worker): record source_fps/source_frames at generation; reset EDL"
```

---

## Task 4: Shot 序列化加「有效播放描述」

**Files:**
- Modify: `backend/app/api/projects.py:165-198`（两处 shot dict：约 165、约 260）
- Test: `backend/tests/test_shot_serialization.py`

**Interfaces:**
- Produces: 每个 shot dict 新增 `trim_frames`、`source_fps`、`source_frames`、`trim_end_sec: float|None`、`vc_audio_url: str|None`
- Consumes: `app.services.storage.to_media_url`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_shot_serialization.py
from app.api.projects import _shot_to_dict
from app.models.project import Shot


def test_shot_to_dict_includes_playback_descriptor():
    s = Shot(
        project_id="p", shot_id=1, text="hi", shot_type="Close-up",
        visual_description="x", shot_duration=4, status="completed",
        trim_frames=60, source_fps=30.0, source_frames=120,
        vc_audio_path="/data/projects/p/shots/shot_1/audio_vc_1_ab.wav",
    )
    d = _shot_to_dict(s)
    assert d["trim_frames"] == 60
    assert d["source_frames"] == 120
    assert abs(d["trim_end_sec"] - 2.0) < 1e-6      # 60 / 30
    assert d["vc_audio_url"].startswith("/api/media/")


def test_trim_end_sec_none_when_no_trim():
    s = Shot(project_id="p", shot_id=1, text="t", shot_type="x",
             visual_description="x", shot_duration=4, status="completed",
             trim_frames=None, source_fps=30.0, source_frames=120)
    assert _shot_to_dict(s)["trim_end_sec"] is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest backend/tests/test_shot_serialization.py -v`
Expected: FAIL（ImportError: `_shot_to_dict`）

- [ ] **Step 3: 抽出共享序列化器并接入两处**

在 `backend/app/api/projects.py` 顶部（import 之后）加共享函数：

```python
def _shot_to_dict(s) -> dict:
    """Serialize a Shot for the API, including the non-destructive playback descriptor."""
    import json
    trim_end_sec = None
    if s.trim_frames and s.source_fps:
        trim_end_sec = s.trim_frames / s.source_fps
    return {
        "id": s.id,
        "shot_id": s.shot_id,
        "text": s.text,
        "shot_type": s.shot_type,
        "visual_description": s.visual_description,
        "shot_duration": s.shot_duration,
        "status": s.status,
        "align_with_previous": s.align_with_previous,
        "motion_prompt": s.motion_prompt,
        "first_frame_path": to_media_url(s.first_frame_path),
        "video_path": to_media_url(s.video_path),
        "last_frame_path": to_media_url(s.last_frame_path),
        "word_count_warning": s.word_count_warning,
        "error_message": s.error_message,
        "custom_first_frame_path": to_media_url(s.custom_first_frame_path),
        "custom_reference_paths": (
            [to_media_url(p) for p in json.loads(s.custom_reference_paths)]
            if s.custom_reference_paths else None
        ),
        "reference_image_hint": s.reference_image_hint,
        "use_prev_last_frame": s.use_prev_last_frame,
        "vc_status": s.vc_status,
        "vc_error_message": s.vc_error_message,
        "cc_status": s.cc_status,
        "cc_error_message": s.cc_error_message,
        "target_last_frame_path": to_media_url(s.target_last_frame_path),
        "tf_status": s.tf_status,
        "tf_error_message": s.tf_error_message,
        "tf_confirmed": bool(s.tf_confirmed),
        # --- 非破坏式播放描述 ---
        "trim_frames": s.trim_frames,
        "source_fps": s.source_fps,
        "source_frames": s.source_frames,
        "trim_end_sec": trim_end_sec,
        "vc_audio_url": to_media_url(s.vc_audio_path),
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }
```

把两处内联的 `{ "id": s.id, ... }` 列表推导替换为 `[_shot_to_dict(s) for s in project.shots]`（约 165 行）和另一处（约 260 行；该处若键略有差异，以 `_shot_to_dict` 为准统一）。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --project backend pytest backend/tests/test_shot_serialization.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/projects.py backend/tests/test_shot_serialization.py
git commit -m "feat(api): shot playback descriptor (trim_end_sec, vc_audio_url)"
```

---

## Task 5: 裁剪端点 → 仅改元数据

**Files:**
- Modify: `backend/app/api/pipeline.py:1355-1417`（`trim_shot_video`）
- Modify: `backend/app/models/schemas.py:86-87`（`ShotTrimRequest`，确认 `end_frame`）
- Test: `backend/tests/test_trim_nondestructive.py`

**Interfaces:**
- Consumes: `extract_frame_at`、`shot_source_path`、`get_video_info`
- Produces: `POST /trim` 设 `shot.trim_frames`，源 mp4 不变，重抽 last_frame，重置 CC，**不碰 VC**
- Produces: 共享 fixtures `seeded_shot` / `seeded_shot_factory` / `seeded_vc_done`（Task 6/7/8 复用）

- [ ] **Step 0: 建共享测试 fixtures（Task 6/7/8 也用）**

```python
# backend/tests/conftest.py （若已存在则追加这些 fixture）
import subprocess
import pytest
import pytest_asyncio
from pathlib import Path
from sqlalchemy import select

from app.db import init_db, get_session_factory  # 若无 get_session_factory，用项目现有 session 工厂
from app.models.project import Project, Shot
from app.services.storage import shot_dir, ts_uuid_name
from app.config import settings


def _make_source_mp4(project_id: str, shot_id: int) -> Path:
    s_dir = shot_dir(project_id, shot_id)
    s_dir.mkdir(parents=True, exist_ok=True)
    out = s_dir / f"output_{ts_uuid_name('.mp4')}"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440", "-frames:v", "120",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(out)],
        check=True, capture_output=True)
    return out


@pytest_asyncio.fixture
async def seeded_shot(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    await init_db()
    sf = get_session_factory()
    project_id = "test-proj"
    async with sf() as s:
        s.add(Project(id=project_id, status="completed"))
        await s.commit()
    src = _make_source_mp4(project_id, 1)
    from app.agents.video_trimmer import get_video_info
    info = get_video_info(str(src))
    async with sf() as s:
        s.add(Shot(project_id=project_id, shot_id=1, text="hi", shot_type="Close-up",
                   visual_description="x", shot_duration=4, status="completed",
                   video_path=str(src), source_fps=info["fps"],
                   source_frames=info["total_frames"]))
        await s.commit()
    return project_id, 1, src


@pytest.fixture
def seeded_shot_factory(seeded_shot):
    project_id, shot_id, src = seeded_shot
    def _factory():
        return get_session_factory(), None, project_id, shot_id, src
    return _factory


@pytest_asyncio.fixture
async def seeded_vc_done(seeded_shot):
    project_id, shot_id, src = seeded_shot
    sf = get_session_factory()
    wav = src.parent / f"audio_vc_{ts_uuid_name('.wav')}"
    wav.write_bytes(b"RIFFfakewav")
    async with sf() as s:
        shot = (await s.execute(select(Shot).where(
            Shot.project_id == project_id, Shot.shot_id == shot_id))).scalar_one()
        shot.vc_status = "done"
        shot.vc_audio_path = str(wav)
        await s.commit()
    return project_id, shot_id, wav, src
```

> 适配点：用项目现有的 session 工厂获取方式（`grep -rn "sessionmaker\|async_session\|get_session" backend/app/db.py`）替换 `get_session_factory()`；`_do_voice_convert_one` 的 `redis` 参数在测试里用一个支持 `await publish_event` 的桩（或把 `publish_event` patch 成 no-op）。`X-User` 头名以现有 `_require_user` 实现为准（`grep -n "_require_user" backend/app/api/pipeline.py`）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_trim_nondestructive.py
import hashlib
import subprocess
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.db import get_session


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_trim_sets_metadata_and_keeps_source_immutable(seeded_shot):
    """seeded_shot fixture: a project+shot with a real output_*.mp4 on disk,
    status=completed, source_fps/source_frames filled. (见 conftest 说明)"""
    project_id, shot_id, source_mp4 = seeded_shot
    before = _md5(source_mp4)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(
            f"/api/projects/{project_id}/shots/{shot_id}/trim",
            json={"end_frame": 40},
            headers={"X-User": "tester"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["trim_frames"] == 40

    # 源视频字节级不变
    assert _md5(source_mp4) == before
    # 没有 trimmed_ 文件产生
    assert not list(source_mp4.parent.glob("trimmed_*.mp4"))
```

> conftest 需提供 `seeded_shot` fixture：建一个 project + 一个 completed shot，往 `shot_dir` 写一个真实 `output_<ts>_<uuid>.mp4`（用 `ffmpeg testsrc2 -frames:v 120 -c:v libx264`），并把 `video_path`/`source_fps`/`source_frames` 写进 DB。复用现有测试 DB 夹具（若无则在 conftest 用临时 sqlite + `init_db()`）。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest backend/tests/test_trim_nondestructive.py -v`
Expected: FAIL（当前实现会生成 trimmed_ 并改 video_path → 断言失败）

- [ ] **Step 3: 重写 `trim_shot_video`**

把 `backend/app/api/pipeline.py` 的 `trim_shot_video` 函数体（1363 起）替换为：

```python
    """Non-destructive trim: record trim_frames and refresh the last frame.

    The source output_*.mp4 is never modified. Trimming changes the last frame
    (index N-1 of the source) → re-extract it and reset CC. VC is untouched
    (the vc audio is full-length and independent of trim length).
    """
    from app.agents.video_trimmer import get_video_info
    from app.agents.frame_porter import extract_frame_at
    from app.services.storage import (
        shot_source_path, ts_uuid_name, shot_dir, shot_pre_cc_last_frame_path,
    )

    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot or not shot.video_path:
        raise HTTPException(status_code=404, detail="Shot or video not found")
    if shot.status != "completed":
        raise HTTPException(status_code=409, detail="Shot is not completed")

    source = shot_source_path(project_id, shot_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source video not found")
    info = get_video_info(str(source))
    total = info["total_frames"]

    if body.end_frame < 24:
        raise HTTPException(status_code=400, detail="Must keep at least 24 frames")
    n = min(body.end_frame, total)  # clamp; full length is a no-op trim

    # 1. metadata only
    shot.trim_frames = n if n < total else None
    shot.video_path = str(source)            # always the immutable source
    shot.source_fps = info["fps"]
    shot.source_frames = total

    # 2. refresh last frame = source frame N-1 (or full last frame when no trim)
    s_dir = shot_dir(project_id, shot_id)
    for _old in list(s_dir.glob("last_frame_*.png")) + list(s_dir.glob("cc_*.png")):
        _old.unlink(missing_ok=True)
    new_lf = s_dir / f"last_frame_{ts_uuid_name('.png')}"
    extract_frame_at(str(source), (n - 1) if n < total else (total - 1), str(new_lf))
    shot.last_frame_path = str(new_lf)
    await _repoint_next_first_frame(project_id, shot.shot_id, str(new_lf), session)

    # 3. last frame changed → reset CC (image really changed). VC untouched.
    shot.cc_status = None
    shot.cc_error_message = None
    pre_cc = shot_pre_cc_last_frame_path(project_id, shot_id)
    if pre_cc.exists():
        pre_cc.unlink()

    ts = int(datetime.utcnow().timestamp())
    await session.commit()
    return {
        "video_path": to_media_url(shot.video_path),
        "last_frame_path": to_media_url(shot.last_frame_path),
        "trim_frames": shot.trim_frames,
        "version": ts,
        **get_video_info(str(source)),
    }
```

确认 `ShotTrimRequest.end_frame` 仍是 `int = Field(..., ge=1)`（`schemas.py:87`），无需改。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --project backend pytest backend/tests/test_trim_nondestructive.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/pipeline.py backend/tests/test_trim_nondestructive.py
git commit -m "feat(api): non-destructive trim (metadata + last-frame refresh, source immutable)"
```

---

## Task 6: restore-trim → 清 trim_frames

**Files:**
- Modify: `backend/app/api/pipeline.py:1420-1472`（`restore_trim`）
- Test: `backend/tests/test_trim_nondestructive.py`（追加）

**Interfaces:**
- Produces: `POST /restore-trim` 设 `trim_frames=None`，重抽全长末帧，重置 CC

- [ ] **Step 1: 追加失败测试**

```python
@pytest.mark.asyncio
async def test_restore_clears_trim(seeded_shot):
    project_id, shot_id, source_mp4 = seeded_shot
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        await ac.post(f"/api/projects/{project_id}/shots/{shot_id}/trim",
                      json={"end_frame": 40}, headers={"X-User": "tester"})
        r = await ac.post(f"/api/projects/{project_id}/shots/{shot_id}/restore-trim",
                          headers={"X-User": "tester"})
    assert r.status_code == 200
    assert r.json()["trim_frames"] is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest backend/tests/test_trim_nondestructive.py::test_restore_clears_trim -v`
Expected: FAIL（旧实现找不到 trimmed_ → 404 / 或键缺失）

- [ ] **Step 3: 重写 `restore_trim`**

把 `restore_trim` 函数体（1427 起）替换为：

```python
    """Clear the trim: trim_frames=None, refresh last frame to the source's final frame."""
    from app.agents.video_trimmer import get_video_info
    from app.agents.frame_porter import extract_frame_at
    from app.services.storage import (
        shot_source_path, ts_uuid_name, shot_dir, shot_pre_cc_last_frame_path,
    )

    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot or not shot.video_path:
        raise HTTPException(status_code=404, detail="Shot or video not found")

    source = shot_source_path(project_id, shot_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source video not found")
    info = get_video_info(str(source))
    total = info["total_frames"]

    shot.trim_frames = None
    shot.video_path = str(source)
    shot.source_fps = info["fps"]
    shot.source_frames = total

    s_dir = shot_dir(project_id, shot_id)
    for _old in list(s_dir.glob("last_frame_*.png")) + list(s_dir.glob("cc_*.png")):
        _old.unlink(missing_ok=True)
    new_lf = s_dir / f"last_frame_{ts_uuid_name('.png')}"
    extract_frame_at(str(source), total - 1, str(new_lf))
    shot.last_frame_path = str(new_lf)
    await _repoint_next_first_frame(project_id, shot.shot_id, str(new_lf), session)

    shot.cc_status = None
    shot.cc_error_message = None
    pre_cc = shot_pre_cc_last_frame_path(project_id, shot_id)
    if pre_cc.exists():
        pre_cc.unlink()

    ts = int(datetime.utcnow().timestamp())
    await session.commit()
    return {
        "video_path": to_media_url(shot.video_path),
        "last_frame_path": to_media_url(shot.last_frame_path),
        "trim_frames": None,
        "version": ts,
        **get_video_info(str(source)),
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --project backend pytest backend/tests/test_trim_nondestructive.py -v`
Expected: PASS（全部）

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/pipeline.py backend/tests/test_trim_nondestructive.py
git commit -m "feat(api): restore-trim clears trim_frames (metadata-only)"
```

---

## Task 7: VC worker → 只产 audio_vc wav

**Files:**
- Modify: `backend/worker/tasks.py:940-1008`（`_do_voice_convert_one`）
- Test: `backend/tests/test_vc_nondestructive.py`

**Interfaces:**
- Consumes: `extract_audio_wav`、`voice_convert`（mock）、`get_original_video_for_audio`、`shot_audio_vc_path`/`ts_uuid_name`
- Produces: VC 后 `shot.vc_audio_path` 指向新 `audio_vc_<ts>_<uuid>.wav`，`vc_status="done"`，源 mp4 与 `video_path` 不变，无 `vc_*.mp4`

- [ ] **Step 1: 写失败测试（mock CosyVoice）**

```python
# backend/tests/test_vc_nondestructive.py
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from worker.tasks import _do_voice_convert_one


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_vc_writes_wav_only_keeps_source(seeded_shot_factory):
    """seeded_shot_factory: 同 seeded_shot，但返回可注入 session_factory/redis 的工厂。"""
    sf, redis, project_id, shot_id, source_mp4 = seeded_shot_factory()
    before = _md5(source_mp4)

    async def fake_vc(src, ref, out):
        Path(out).write_bytes(b"RIFFfakewav")   # 占位 wav，避免真实计费

    with patch("app.services.cosyvoice_client.voice_convert", new=AsyncMock(side_effect=fake_vc)), \
         patch("app.agents.audio_extractor.extract_audio_wav", return_value=None) as ex:
        # extract_audio_wav 仅需产出一个文件供 fake_vc 读取的前置；这里桩掉
        ex.side_effect = lambda v, o: Path(o).write_bytes(b"src")
        await _do_voice_convert_one(sf, redis, project_id, shot_id, "/tmp/ref.wav")

    async with sf() as s:
        from sqlalchemy import select
        from app.models.project import Shot
        shot = (await s.execute(select(Shot).where(
            Shot.project_id == project_id, Shot.shot_id == shot_id))).scalar_one()
        assert shot.vc_status == "done"
        assert shot.vc_audio_path and Path(shot.vc_audio_path).name.startswith("audio_vc_")
        assert Path(shot.vc_audio_path).exists()
        assert shot.video_path == str(source_mp4)          # video_path 仍是源
    assert _md5(source_mp4) == before                       # 源不变
    assert not list(source_mp4.parent.glob("vc_*.mp4"))     # 无 vc_ 视频
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest backend/tests/test_vc_nondestructive.py -v`
Expected: FAIL（旧实现 remux 出 vc_ 并改 video_path）

- [ ] **Step 3: 重写 `_do_voice_convert_one` 的 try 块**

把 `_do_voice_convert_one` 的 `try:` 内 1~5 步（约 970-994）替换为：

```python
        try:
            # 1. Extract full source audio (trim-independent)
            source_video = get_original_video_for_audio(project_id, shot_id)
            src_audio = str(shot_dir(project_id, shot_id) / f"audio_in_{ts_uuid_name('.wav')}")
            extract_audio_wav(str(source_video), src_audio)

            # 2. CosyVoice VC → a NEW uniquely-named wav (never overwrite)
            vc_audio = str(shot_dir(project_id, shot_id) / f"audio_vc_{ts_uuid_name('.wav')}")
            await voice_convert(src_audio, ref_audio_path, vc_audio)

            # 3. Metadata only: drop a prior vc audio, point at the new one.
            #    Source video is NOT touched; video_path stays the source.
            if shot.vc_audio_path:
                Path(shot.vc_audio_path).unlink(missing_ok=True)
            Path(src_audio).unlink(missing_ok=True)
            shot.vc_audio_path = vc_audio
            shot.vc_status = "done"
            shot.vc_error_message = None
            session.add(shot)
            await session.commit()
```

并把该函数 import 行（952）改为去掉 `remux_video_with_audio`：

```python
    from app.agents.audio_extractor import extract_audio_wav
    from app.services.cosyvoice_client import voice_convert
```

确认 `ts_uuid_name`、`shot_dir` 已在 tasks.py 顶部从 storage import（第 34 行已含）。VC 完成事件（约 997-1007）把 `"video_path": to_media_url(str(video_path))` 改为 `"vc_audio_url": to_media_url(vc_audio)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --project backend pytest backend/tests/test_vc_nondestructive.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/worker/tasks.py backend/tests/test_vc_nondestructive.py
git commit -m "feat(worker): VC produces audio_vc wav only; source video untouched"
```

---

## Task 8: voice-revert → 清 vc_audio_path

**Files:**
- Modify: `backend/app/api/voice.py:241-287`（`voice_revert_shot`），`:23` import
- Test: `backend/tests/test_vc_nondestructive.py`（追加）

**Interfaces:**
- Produces: `POST /voice-revert` 清 `vc_audio_path`+`vc_status`，删 wav，源不变

- [ ] **Step 1: 追加失败测试**

```python
@pytest.mark.asyncio
async def test_voice_revert_clears_audio(seeded_vc_done):
    """seeded_vc_done: 一个 vc_status='done'、vc_audio_path 指向真实 wav 的 shot。"""
    project_id, shot_id, wav_path, source_mp4 = seeded_vc_done
    import hashlib
    before = hashlib.md5(source_mp4.read_bytes()).hexdigest()
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(f"/api/projects/{project_id}/shots/{shot_id}/voice-revert",
                          headers={"X-User": "tester"})
    assert r.status_code == 200
    assert r.json()["vc_status"] is None
    assert not Path(wav_path).exists()
    assert hashlib.md5(source_mp4.read_bytes()).hexdigest() == before
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest backend/tests/test_vc_nondestructive.py::test_voice_revert_clears_audio -v`
Expected: FAIL（旧实现操作 vc_ 视频）

- [ ] **Step 3: 重写 `voice_revert_shot` 主体**

把 `voice_revert_shot` 中 `if shot.vc_status != "done"` 之后到 commit 之间（约 260-279）替换为：

```python
    if shot.vc_status != "done":
        raise HTTPException(status_code=400, detail="Shot has not been voice-converted")

    # Non-destructive: just drop the vc audio + clear the pointer. video_path
    # already points at the immutable source, so nothing else changes.
    if shot.vc_audio_path:
        Path(shot.vc_audio_path).unlink(missing_ok=True)
    shot.vc_audio_path = None
    shot.vc_status = None
    shot.vc_error_message = None
    session.add(shot)
    await session.commit()
```

把返回值（281-287）改为：

```python
    ts = int(datetime.utcnow().timestamp())
    return {
        "shot_id": shot_id,
        "vc_status": None,
        "vc_audio_url": None,
        "version": ts,
    }
```

并把 `voice.py:23` 的 import 去掉 `shot_pre_vc_video_path`（不再使用）：`from app.services.storage import to_media_url`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --project backend pytest backend/tests/test_vc_nondestructive.py -v`
Expected: PASS（全部）

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/voice.py backend/tests/test_vc_nondestructive.py
git commit -m "feat(api): voice-revert clears vc_audio_path (metadata-only)"
```

---

## Task 9: `effective_clip` 模块（导出现烤核心）

**Files:**
- Create: `backend/app/agents/effective_clip.py`
- Test: `backend/tests/test_effective_clip.py`

**Interfaces:**
- Produces: `build_effective_clip(source_path: str, *, trim_frames: int|None, vc_audio_path: str|None, out_path: str, vcodec: str = "libx264", crf: int = 18, acodec: str = "aac") -> None`
- Produces: `effective_clip_paths(shots: list, tmp_dir: str) -> list[str]`（未编辑→源路径透传；已编辑→烤到 tmp_dir 的临时文件）
- Consumes: `app.services.storage.shot_source_path`

- [ ] **Step 1: 写失败测试（含 md5 末帧一致性）**

```python
# backend/tests/test_effective_clip.py
import hashlib
import subprocess
from pathlib import Path

import pytest

from app.agents.effective_clip import build_effective_clip
from app.agents.frame_porter import extract_frame_at
from app.agents.video_trimmer import get_video_info


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


@pytest.fixture
def lossless_src(tmp_path):
    out = tmp_path / "src.mkv"   # mkv 容器装 ffv1
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
         "-frames:v", "120", "-c:v", "ffv1", "-c:a", "pcm_s16le", "-shortest", str(out)],
        check=True, capture_output=True,
    )
    return out


def test_trim_only_frame_count(lossless_src, tmp_path):
    out = tmp_path / "clip.mkv"
    build_effective_clip(str(lossless_src), trim_frames=60, vc_audio_path=None,
                         out_path=str(out), vcodec="ffv1", acodec="pcm_s16le")
    assert get_video_info(str(out))["total_frames"] == 60


def test_trim_last_frame_md5_matches_source(lossless_src, tmp_path):
    """核心：烤出的 clip 的末帧 == 源第 59 帧（无损 → 严格 md5）。"""
    out = tmp_path / "clip.mkv"
    build_effective_clip(str(lossless_src), trim_frames=60, vc_audio_path=None,
                         out_path=str(out), vcodec="ffv1", acodec="pcm_s16le")
    clip_last = tmp_path / "clip_last.png"
    src_n_minus_1 = tmp_path / "src59.png"
    extract_frame_at(str(out), 59, str(clip_last))
    extract_frame_at(str(lossless_src), 59, str(src_n_minus_1))
    assert _md5(clip_last) == _md5(src_n_minus_1)


def test_no_edit_passthrough(lossless_src, tmp_path):
    """未编辑：build 直接拷贝/透传，帧数不变。"""
    out = tmp_path / "clip.mkv"
    build_effective_clip(str(lossless_src), trim_frames=None, vc_audio_path=None,
                         out_path=str(out), vcodec="ffv1", acodec="pcm_s16le")
    assert get_video_info(str(out))["total_frames"] == 120
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest backend/tests/test_effective_clip.py -v`
Expected: FAIL（ModuleNotFoundError: effective_clip）

- [ ] **Step 3: 实现模块**

```python
# backend/app/agents/effective_clip.py
"""Bake a shot's effective clip from the immutable source + EDL metadata.

The single place ffmpeg applies trim / audio-substitution. Used by the merger
at export time; preview compositing is done independently on the frontend from
the same DB metadata (trim_frames, vc_audio_path).
"""

import logging
import shutil
from pathlib import Path

from ffmpeg import FFmpeg

from app.services.storage import shot_source_path, ts_uuid_name

logger = logging.getLogger(__name__)


def build_effective_clip(
    source_path: str,
    *,
    trim_frames: int | None,
    vc_audio_path: str | None,
    out_path: str,
    vcodec: str = "libx264",
    crf: int = 18,
    acodec: str = "aac",
) -> None:
    """Render <source> with trim + audio-substitution applied into out_path.

    - trim_frames: keep frames 0..trim_frames-1 (frame-precise via -frames:v);
      -shortest bounds the audio stream to the trimmed video length.
    - vc_audio_path: replace the audio with this full-length wav (clamped by -shortest).
    - No edits → straight copy of the source bytes.
    """
    if not trim_frames and not vc_audio_path:
        shutil.copy2(source_path, out_path)
        return

    ff = FFmpeg().option("y").input(source_path)
    audio_map = "0:a"
    if vc_audio_path:
        ff = ff.input(vc_audio_path)
        audio_map = "1:a"

    opts: dict = {"map": ["0:v", audio_map], "vcodec": vcodec, "acodec": acodec}
    if vcodec == "libx264":
        opts["preset"] = "fast"
        opts["crf"] = crf
    if trim_frames:
        opts["frames:v"] = trim_frames
        opts["shortest"] = None  # stop audio when the trimmed video ends

    ff.output(out_path, **opts).execute()
    if not Path(out_path).exists():
        raise RuntimeError(f"build_effective_clip produced no output: {out_path}")
    logger.info(
        "Effective clip %s (trim=%s vc=%s)", out_path, trim_frames, bool(vc_audio_path)
    )


def effective_clip_paths(shots: list, tmp_dir: str) -> list[str]:
    """Return one playable path per shot: source passthrough if unedited, else a
    freshly-baked temp clip under tmp_dir. Caller owns tmp_dir cleanup.

    Each shot must expose .project_id, .shot_id, .trim_frames, .vc_audio_path.
    """
    out: list[str] = []
    for s in shots:
        source = shot_source_path(s.project_id, s.shot_id)
        if source is None:
            raise FileNotFoundError(f"Shot {s.shot_id}: no source video")
        if not s.trim_frames and not s.vc_audio_path:
            out.append(str(source))
            continue
        clip = str(Path(tmp_dir) / f"eff_{s.shot_id}_{ts_uuid_name('.mp4')}")
        build_effective_clip(
            str(source),
            trim_frames=s.trim_frames,
            vc_audio_path=s.vc_audio_path,
            out_path=clip,
        )
        out.append(clip)
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --project backend pytest backend/tests/test_effective_clip.py -v`
Expected: PASS（3 passed，含末帧 md5）

- [ ] **Step 5: Commit**

```bash
git add backend/app/agents/effective_clip.py backend/tests/test_effective_clip.py
git commit -m "feat(export): build_effective_clip + effective_clip_paths (trim+VC bake)"
```

---

## Task 10: `run_merger` 用 effective clips

**Files:**
- Modify: `backend/worker/tasks.py:880-899`（`run_merger` 取 path 段）
- Test: `backend/tests/test_merger_effective.py`

**Interfaces:**
- Consumes: `effective_clip_paths`、现有 `merge_shots_with_crossfade`
- Produces: 导出从 effective clips 合并；临时文件用后清理

- [ ] **Step 1: 写失败测试（端到端 md5，单镜头流拷贝路径）**

```python
# backend/tests/test_merger_effective.py
import hashlib
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.effective_clip import effective_clip_paths, build_effective_clip
from app.agents.frame_porter import extract_frame_at
from app.agents.merger import merge_shots_with_crossfade


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def test_single_shot_export_last_frame_md5(tmp_path, monkeypatch):
    """单镜头导出走 c=copy；用无损烤片 → 最终视频末帧 == 源第 N-1 帧（严格 md5）。"""
    src = tmp_path / "out.mkv"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440", "-frames:v", "120",
         "-c:v", "ffv1", "-c:a", "pcm_s16le", "-shortest", str(src)],
        check=True, capture_output=True,
    )
    # 直接烤一个 trim=60 的无损 effective clip
    clip = tmp_path / "eff.mkv"
    build_effective_clip(str(src), trim_frames=60, vc_audio_path=None,
                         out_path=str(clip), vcodec="ffv1", acodec="pcm_s16le")
    # 单输入 merge 走 c=copy → 字节保真
    final = tmp_path / "final.mkv"
    merge_shots_with_crossfade([str(clip)], str(final), crossfade_duration=0.3)

    f_last = tmp_path / "f.png"
    s59 = tmp_path / "s.png"
    extract_frame_at(str(final), 59, str(f_last))
    extract_frame_at(str(src), 59, str(s59))
    assert _md5(f_last) == _md5(s59)
```

- [ ] **Step 2: 跑测试确认通过（验证合成不变量；此测试不依赖 run_merger 改动）**

Run: `uv run --project backend pytest backend/tests/test_merger_effective.py -v`
Expected: PASS（锁住「无损链路下末帧 md5 一致」这一核心不变量）

- [ ] **Step 3: 改 `run_merger` 用 effective clips**

把 `backend/worker/tasks.py` `run_merger` 中 `shot_paths = [s.video_path ...]` 到 `merge_shots_with_crossfade(...)` 之间替换为：

```python
        if not shots:
            raise ValueError("No completed shots to merge")

        final_path = final_video_path(project_id)
        final_path.parent.mkdir(parents=True, exist_ok=True)

        import tempfile, shutil as _shutil
        from app.agents.effective_clip import effective_clip_paths
        tmp_dir = tempfile.mkdtemp(prefix=f"export_{project_id}_")
        try:
            shot_paths = effective_clip_paths(list(shots), tmp_dir)
            if not shot_paths:
                raise ValueError("No completed shots to merge")
            cf = crossfade_duration if crossfade_duration is not None else settings.crossfade_duration
            merge_shots_with_crossfade(shot_paths, str(final_path), crossfade_duration=cf)

            project.final_video_path = str(final_path)
            session.add(project)
            await transition_project_status(
                project, ProjectStatus.EXPORTED, "system:worker", session, redis
            )
            await publish_event(
                redis, project_id,
                {"type": "export_done",
                 "data": {"final_video_path": f"/api/projects/{project_id}/final.mp4"}},
            )
            logger.info(f"Merger completed for project {project_id}")
        except Exception as e:
            logger.error(f"Merger failed for project {project_id}: {e}")
            project.error_message = str(e)
            session.add(project)
            await transition_project_status(
                project, ProjectStatus.FAILED, "system:worker", session, redis
            )
            await publish_event(
                redis, project_id,
                {"type": "pipeline_failed", "data": {"error_message": str(e)}},
            )
        finally:
            _shutil.rmtree(tmp_dir, ignore_errors=True)
```

> 注意：删除原有的 `shot_paths = [...]`、`final_path = ...`、以及原 `try/except` 块，避免重复定义。

- [ ] **Step 4: 语法校验 + 全后端回归**

Run: `uv run --project backend python -c "import ast; ast.parse(open('backend/worker/tasks.py').read()); print('ok')"`
Then: `uv run --project backend pytest backend/tests/test_merger_effective.py backend/tests/test_effective_clip.py -v`
Expected: `ok` + PASS

- [ ] **Step 5: Commit**

```bash
git add backend/worker/tasks.py backend/tests/test_merger_effective.py
git commit -m "feat(export): run_merger bakes effective clips from EDL with tmp cleanup"
```

---

## Task 11: 端到端 md5 + 源不可变回归测试

**Files:**
- Create: `backend/tests/test_nondestructive_invariants.py`

**Interfaces:**
- Consumes: 上述全部

- [ ] **Step 1: 写测试**

```python
# backend/tests/test_nondestructive_invariants.py
"""锁住核心不变量：源帧 N-1 == 分镜 last_frame == 烤片末帧（无损链路严格 md5）。"""
import hashlib
import subprocess
from pathlib import Path

import pytest

from app.agents.effective_clip import build_effective_clip
from app.agents.frame_porter import extract_frame_at


def _md5(p): return hashlib.md5(Path(p).read_bytes()).hexdigest()


@pytest.fixture
def src(tmp_path):
    out = tmp_path / "out.mkv"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=30",
         "-frames:v", "120", "-c:v", "ffv1", str(out)],
        check=True, capture_output=True)
    return out


@pytest.mark.parametrize("n", [30, 60, 90])
def test_trim_last_frame_equals_source_frame(src, tmp_path, n):
    # 模拟 trim 端点的抽帧逻辑：源第 n-1 帧
    lf = tmp_path / f"lf_{n}.png"
    extract_frame_at(str(src), n - 1, str(lf))
    # effective clip 末帧（无损）
    clip = tmp_path / f"clip_{n}.mkv"
    build_effective_clip(str(src), trim_frames=n, vc_audio_path=None,
                         out_path=str(clip), vcodec="ffv1", acodec="pcm_s16le")
    clip_last = tmp_path / f"cl_{n}.png"
    extract_frame_at(str(clip), n - 1, str(clip_last))
    assert _md5(lf) == _md5(clip_last)


def test_build_does_not_modify_source(src, tmp_path):
    before = _md5(src)
    clip = tmp_path / "c.mkv"
    build_effective_clip(str(src), trim_frames=60, vc_audio_path=None,
                         out_path=str(clip), vcodec="ffv1", acodec="pcm_s16le")
    assert _md5(src) == before
```

- [ ] **Step 2: 跑测试确认通过**

Run: `uv run --project backend pytest backend/tests/test_nondestructive_invariants.py -v`
Expected: PASS（4 passed）

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_nondestructive_invariants.py
git commit -m "test: end-to-end md5 invariants (source frame == last_frame == clip last frame)"
```

---

## Task 12: 前端 `useShotSync` hook

**Files:**
- Create: `frontend-vite/src/hooks/useShotSync.ts`
- Test: `frontend-vite/src/components/__tests__/useShotSync.test.tsx`

**Interfaces:**
- Produces: `useShotSync(opts: { trimEndSec: number | null; audioEnabled: boolean }) -> { videoRef, audioRef, onPlay, onPause, onSeeked, onTimeUpdate }`

- [ ] **Step 1: 写失败测试**

```tsx
// frontend-vite/src/components/__tests__/useShotSync.test.tsx
import { renderHook } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { useShotSync } from '../../hooks/useShotSync'

describe('useShotSync', () => {
  it('pauses both elements when video passes trimEndSec', () => {
    const { result } = renderHook(() =>
      useShotSync({ trimEndSec: 2.0, audioEnabled: true }))
    const video = { currentTime: 2.5, pause: vi.fn() }
    const audio = { currentTime: 2.5, pause: vi.fn(), play: vi.fn() }
    // @ts-expect-error test doubles
    result.current.videoRef.current = video
    // @ts-expect-error test doubles
    result.current.audioRef.current = audio
    result.current.onTimeUpdate()
    expect(video.pause).toHaveBeenCalled()
    expect(audio.pause).toHaveBeenCalled()
  })

  it('corrects audio drift > 0.15s on timeupdate', () => {
    const { result } = renderHook(() =>
      useShotSync({ trimEndSec: null, audioEnabled: true }))
    const video = { currentTime: 1.0, pause: vi.fn() }
    const audio = { currentTime: 1.3, pause: vi.fn(), play: vi.fn() }
    // @ts-expect-error test doubles
    result.current.videoRef.current = video
    // @ts-expect-error test doubles
    result.current.audioRef.current = audio
    result.current.onTimeUpdate()
    expect(audio.currentTime).toBeCloseTo(1.0)
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/useShotSync.test.tsx`
Expected: FAIL（找不到模块）

- [ ] **Step 3: 实现 hook**

```ts
// frontend-vite/src/hooks/useShotSync.ts
import { useRef, useCallback } from 'react'

const DRIFT_TOLERANCE = 0.15

export interface ShotSyncOptions {
  trimEndSec: number | null
  audioEnabled: boolean
}

/** Keeps a muted <video> (picture) and an <audio> (vc track) in sync, and
 *  clamps playback to trimEndSec. video is the master clock. */
export function useShotSync({ trimEndSec, audioEnabled }: ShotSyncOptions) {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  const onPlay = useCallback(() => {
    if (audioEnabled) audioRef.current?.play?.()
  }, [audioEnabled])

  const onPause = useCallback(() => {
    audioRef.current?.pause?.()
  }, [])

  const onSeeked = useCallback(() => {
    const v = videoRef.current
    const a = audioRef.current
    if (v && a) a.currentTime = v.currentTime
  }, [])

  const onTimeUpdate = useCallback(() => {
    const v = videoRef.current
    const a = audioRef.current
    if (!v) return
    if (trimEndSec != null && v.currentTime >= trimEndSec) {
      v.pause()
      a?.pause?.()
      return
    }
    if (audioEnabled && a && Math.abs(a.currentTime - v.currentTime) > DRIFT_TOLERANCE) {
      a.currentTime = v.currentTime
    }
  }, [trimEndSec, audioEnabled])

  return { videoRef, audioRef, onPlay, onPause, onSeeked, onTimeUpdate }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/useShotSync.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend-vite/src/hooks/useShotSync.ts frontend-vite/src/components/__tests__/useShotSync.test.tsx
git commit -m "feat(ui): useShotSync hook (video/audio sync + trim clamp + drift)"
```

---

## Task 13: `ShotPlayer` 组件（三模式 + A/B 开关）

**Files:**
- Create: `frontend-vite/src/components/ShotPlayer.tsx`
- Test: `frontend-vite/src/components/__tests__/ShotPlayer.test.tsx`

**Interfaces:**
- Consumes: `useShotSync`
- Produces: `<ShotPlayer videoUrl trimEndSec={number|null} audioUrl={string|null} />`

- [ ] **Step 1: 写失败测试**

```tsx
// frontend-vite/src/components/__tests__/ShotPlayer.test.tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { ShotPlayer } from '../ShotPlayer'

describe('ShotPlayer', () => {
  it('mode 1: plain video, no audio element, no toggle', () => {
    const { container } = render(<ShotPlayer videoUrl="/v.mp4" trimEndSec={null} audioUrl={null} />)
    expect(container.querySelector('video')).toBeTruthy()
    expect(container.querySelector('audio')).toBeNull()
    expect(screen.queryByTestId('ab-toggle')).toBeNull()
  })

  it('mode 3: muted video + audio element + A/B toggle when audioUrl set', () => {
    const { container } = render(<ShotPlayer videoUrl="/v.mp4" trimEndSec={2} audioUrl="/a.wav" />)
    const video = container.querySelector('video') as HTMLVideoElement
    expect(video.muted).toBe(true)
    expect(container.querySelector('audio')).toBeTruthy()
    expect(screen.getByTestId('ab-toggle')).toBeTruthy()
  })

  it('A/B toggle mutes vc audio and unmutes source', () => {
    const { container } = render(<ShotPlayer videoUrl="/v.mp4" trimEndSec={2} audioUrl="/a.wav" />)
    const video = container.querySelector('video') as HTMLVideoElement
    fireEvent.click(screen.getByTestId('ab-toggle'))   // 切到原音
    expect(video.muted).toBe(false)
    const audio = container.querySelector('audio') as HTMLAudioElement
    expect(audio.muted).toBe(true)
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/ShotPlayer.test.tsx`
Expected: FAIL（找不到模块）

- [ ] **Step 3: 实现组件**

```tsx
// frontend-vite/src/components/ShotPlayer.tsx
import { useState } from 'react'
import { useShotSync } from '../hooks/useShotSync'

export interface ShotPlayerProps {
  videoUrl: string
  trimEndSec: number | null
  audioUrl: string | null
}

/** Non-destructive playback: trims by clamping, substitutes VC audio via a
 *  synced <audio>. A/B toggle switches between vc track and source audio. */
export function ShotPlayer({ videoUrl, trimEndSec, audioUrl }: ShotPlayerProps) {
  const hasVc = !!audioUrl
  const [useVc, setUseVc] = useState(true)        // true = vc track, false = source audio
  const audioEnabled = hasVc && useVc
  const { videoRef, audioRef, onPlay, onPause, onSeeked, onTimeUpdate } =
    useShotSync({ trimEndSec, audioEnabled })

  return (
    <div className="shot-player">
      <video
        ref={videoRef}
        src={videoUrl}
        controls
        muted={audioEnabled}
        onPlay={onPlay}
        onPause={onPause}
        onSeeked={onSeeked}
        onTimeUpdate={onTimeUpdate}
        style={{ width: '100%' }}
      />
      {hasVc && (
        <>
          <audio ref={audioRef} src={audioUrl!} muted={!useVc} preload="auto" />
          <button
            type="button"
            data-testid="ab-toggle"
            onClick={() => setUseVc((v) => !v)}
            className="text-xs px-2 py-1 mt-1 rounded bg-gray-100"
          >
            {useVc ? '🔊 配音' : '🎙 原音'}
          </button>
        </>
      )}
    </div>
  )
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/ShotPlayer.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend-vite/src/components/ShotPlayer.tsx frontend-vite/src/components/__tests__/ShotPlayer.test.tsx
git commit -m "feat(ui): ShotPlayer (3 modes + A/B audio toggle)"
```

---

## Task 14: ShotCard 接入 ShotPlayer

**Files:**
- Modify: `frontend-vite/src/components/ShotCard.tsx:700-712`（播放区 `<video>`）
- Modify: shot 类型定义（找到声明 `video_path` 的 interface，补 `trim_end_sec`/`vc_audio_url`）

**Interfaces:**
- Consumes: `ShotPlayer`、shot 序列化新字段（Task 4）

- [ ] **Step 1: 扩展 shot 类型**

找到前端 Shot 类型（`grep -rn "video_path" frontend-vite/src/types*  frontend-vite/src/**/*.ts*` 定位 interface），补字段：

```ts
  trim_frames?: number | null
  source_fps?: number | null
  source_frames?: number | null
  trim_end_sec?: number | null
  vc_audio_url?: string | null
```

- [ ] **Step 2: 替换播放区 `<video>`**

把 `ShotCard.tsx` 约 700-712 的播放 `<video src=...>` 块替换为：

```tsx
          {shot.video_path && isPlaying && (
            <ShotPlayer
              videoUrl={videoVersion ? `${shot.video_path}?v=${videoVersion}` : shot.video_path}
              trimEndSec={shot.trim_end_sec ?? null}
              audioUrl={shot.vc_audio_url ?? null}
            />
          )}
```

在文件顶部 import：`import { ShotPlayer } from './ShotPlayer'`。

- [ ] **Step 3: 类型检查 + 构建**

Run: `cd frontend-vite && npx tsc --noEmit`
Expected: 无报错（若旧 `onTrimmed` 回调引用 `last_frame_path` 等仍有效，保持不动）

- [ ] **Step 4: 全前端单测**

Run: `cd frontend-vite && npx vitest run`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend-vite/src/components/ShotCard.tsx frontend-vite/src/types*
git commit -m "feat(ui): ShotCard uses ShotPlayer for non-destructive playback"
```

---

## Task 15: Playwright 端到端（mock AI）

**Files:**
- Create: `tests/e2e/nondestructive-playback.spec.ts`

**Interfaces:**
- Consumes: 真实前端 + mock 的 AI 端点

- [ ] **Step 1: 写测试（mock /export、/voice-convert）**

```ts
// tests/e2e/nondestructive-playback.spec.ts
import { test, expect } from '@playwright/test'

test('trimmed shot clamps playback; vc shot shows A/B toggle', async ({ page }) => {
  // 必须 mock 所有 AI 触发端点，避免计费
  await page.route('**/api/projects/*/start', (r) =>
    r.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) }))
  await page.route('**/api/projects/*/export', (r) =>
    r.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) }))
  await page.route('**/api/projects/*/shots/*/voice-convert', (r) =>
    r.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) }))

  // mock 项目数据：一个带 trim_end_sec + vc_audio_url 的 shot
  await page.route('**/api/projects/test-proj', (r) =>
    r.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'test-proj', status: 'completed', shots: [{
          id: 1, shot_id: 1, text: 'hi', shot_type: 'Close-up',
          visual_description: 'x', shot_duration: 4, status: 'completed',
          video_path: '/api/media/projects/test-proj/shots/shot_1/output_1_a.mp4',
          trim_end_sec: 2.0,
          vc_audio_url: '/api/media/projects/test-proj/shots/shot_1/audio_vc_1_a.wav',
        }],
      }),
    }))

  await page.goto('/projects/test-proj')
  // 进入播放
  await page.getByRole('button', { name: /播放|play/i }).first().click().catch(() => {})
  // A/B 开关存在（VC 镜头）
  await expect(page.getByTestId('ab-toggle')).toBeVisible()
})
```

> 路由/选择器按实际前端路由与按钮文案微调；关键是断言 A/B 开关出现，且所有 AI 端点被 mock。

- [ ] **Step 2: 跑测试**

Run: `cd frontend-vite && npx playwright test tests/e2e/nondestructive-playback.spec.ts`
Expected: PASS（如选择器不符则按真实 DOM 调整后通过）

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/nondestructive-playback.spec.ts
git commit -m "test(e2e): non-destructive playback (trim clamp + A/B toggle), AI mocked"
```

---

## Self-Review Notes（规划者自检）

- **Spec 覆盖**：§2 数据模型→Task 1/4；文件命名 prefix+uuid→Task 5/7（`ts_uuid_name`、删旧）；§3 编辑流程→Task 5/6/7/8；裁剪不作废 VC→Task 5（无 VC 重置）；§4 导出→Task 9/10；§5 前端三模式+A/B→Task 12/13/14；§5 测试（端到端末帧 md5 + 源不可变 + mock AI）→Task 9/10/11/15。
- **md5 lossy 处理**：所有严格 md5 测试用无损 fixture（ffv1/pcm + 单镜头 c=copy 路径）；生产仍 libx264。多镜头交叉转场会混合边界帧、且 reencode，故严格 md5 限定在「单镜头/无损」链路；多镜头按帧数/SSIM 容差另测（如需，可后续追加，不在 MVP 阻塞路径）。
- **类型一致**：`build_effective_clip(...)` 关键字签名在 Task 9 定义，Task 10/11 调用一致；`useShotSync` 返回的 ref/handler 名在 Task 12 定义、Task 13 消费一致；序列化键 `trim_end_sec`/`vc_audio_url` 在 Task 4 定义、Task 13/14/15 消费一致。
- **已知跟进（非阻塞）**：CC 维持现状未改；多镜头端到端 md5 需要无损 concat-copy 合并路径（生产不引入，避免混合编码参数风险）。
