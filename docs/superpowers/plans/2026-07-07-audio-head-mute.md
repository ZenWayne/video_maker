# 前段静音（音频剪头）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让分镜视频开头一小段声音静音（非破坏 EDL），视频照播、后段音频时间位置不动。

**Architecture:** 新增 EDL 字段 `Shot.audio_head_mute_frames`；`build_effective_clip` 在音频链末尾加 `volume=enable='lt(t,X)':volume=0` 滤镜；`ShotPlayer`/`useShotSync` 在 `currentTime<headMuteSec` 时静音生效音轨；新增 `detect-speech-start`（找语音起始帧）+ `audio-head-mute` 保存端点；`TrimDialog`/`WaveformTrack` 加波形前手柄。

**Tech Stack:** FastAPI + pytest（后端，真 ffmpeg 单测 skip-if-absent）；React + vitest（前端）；Playwright（e2e）。

## Global Constraints

- 非破坏：新字段是纯 EDL 元数据，不写/改任何素材文件；渲染时在 `build_effective_clip` 一处应用。
- 字段用**帧**：`audio_head_mute_frames`，与 `trim_frames` 对称。序列化附带 `audio_head_mute_sec = frames/source_fps`（source_fps 存在且 frames>0 时，否则 None）。
- 与 `trim_frames`（裁尾）/`vc_audio_path`（换音轨）**正交叠加**，三者任意组合都成立。
- 后端测试用 `uv run --project backend pytest`；真 ffmpeg 测试 `pytest.mark.skipif(shutil.which("ffmpeg") is None)`；绝不 mock 被测数据流，只 mock 计费/模型（本特性无模型调用）。
- 只读检测端点走 `_dialog_source`（源片视角），与 `detect-silence` 一致。
- 改后端代码后 `podman restart video-maker-backend-dev video-maker-worker-dev`。
- 后端全量基线：3 个既有失败（test_motion_prompt_persistence / test_pipeline::test_export_success / test_tail_frame_pipeline::test_confirm_tail_frame_success）；超出即回归。
- 前端基线：vitest 全过；3 个 Playwright e2e spec 文件在 vitest 下 collection 失败是既有噪声。

---

### Task 1: 后端 EDL 字段 + 迁移 + 序列化

**Files:**
- Modify: `backend/app/models/project.py`（Shot，`vc_audio_path` 行附近 ~157）
- Modify: `backend/app/db.py`（`_run_migrations` 的 shots ALTER 列表 ~120）
- Modify: `backend/app/models/schemas.py`（ShotResponse，`trim_end_sec` 附近）
- Modify: `backend/app/api/projects.py`（`_shot_to_dict`，`trim_end_sec` 附近）
- Modify: `backend/app/api/stream.py`（SSE 快照 shot dict，`trim_end_sec` 附近）
- Test: `backend/tests/integration/test_audio_head_mute.py`（新建）

**Interfaces:**
- Produces: `Shot.audio_head_mute_frames: int | None`；序列化字段 `audio_head_mute_frames`、`audio_head_mute_sec: float | None`（Task 2/3/4 依赖）。

- [ ] **Step 1: 写失败测试**

```python
"""前段静音 EDL 字段：落库 + 序列化出 audio_head_mute_sec。"""
import pytest
from sqlalchemy import select
from tests.integration.conftest import HEADERS, _make_project
from app.models.project import Shot


async def test_audio_head_mute_serialized(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    async with db_session_factory() as s:
        s.add(Shot(
            project_id=pid, shot_id=1, text="t", shot_type="Medium Shot",
            visual_description="v", shot_duration=6, status="completed",
            align_with_previous=False, source_fps=24.0, audio_head_mute_frames=12,
        ))
        await s.commit()

    r = await client.get(f"/api/projects/{pid}")
    assert r.status_code == 200
    shot = r.json()["shots"][0]
    assert shot["audio_head_mute_frames"] == 12
    assert shot["audio_head_mute_sec"] == pytest.approx(0.5)  # 12/24


async def test_audio_head_mute_sec_null_without_fps(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    async with db_session_factory() as s:
        s.add(Shot(
            project_id=pid, shot_id=1, text="t", shot_type="Medium Shot",
            visual_description="v", shot_duration=6, status="completed",
            align_with_previous=False, source_fps=None, audio_head_mute_frames=None,
        ))
        await s.commit()

    shot = (await client.get(f"/api/projects/{pid}")).json()["shots"][0]
    assert shot["audio_head_mute_frames"] is None
    assert shot["audio_head_mute_sec"] is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run --project . pytest tests/integration/test_audio_head_mute.py -q`
Expected: FAIL — Shot 无 `audio_head_mute_frames` 属性 / 响应无该键。

- [ ] **Step 3: 实现**

`project.py` 在 `vc_audio_path = Column(...)` 行后加：

```python
    audio_head_mute_frames = Column(Integer, nullable=True)  # 前 [0,N) 帧静音；None/0=不静音
```

`db.py` 的 `_run_migrations` 里，找到 shots 的 `for col, typ in [... ("vc_audio_path", "TEXT"), ...]` 迁移列表，追加一项：

```python
        ("audio_head_mute_frames", "INTEGER"),
```

（若该列表不存在，就在 shots 迁移区仿照 `if not await _has_column("shots", "vc_audio_path"):` 加一个 `audio_head_mute_frames` 的 ALTER。）

在 `schemas.py` 的 `ShotResponse` 里 `trim_end_sec` 附近加：

```python
    audio_head_mute_frames: Optional[int] = None
    audio_head_mute_sec: Optional[float] = None
```

在 `projects.py` 的 `_shot_to_dict` 里 `trim_end_sec` 计算附近加：

```python
    audio_head_mute_sec = None
    if s.audio_head_mute_frames and s.source_fps:
        audio_head_mute_sec = s.audio_head_mute_frames / s.source_fps
```

并在返回 dict 里加两键：

```python
        "audio_head_mute_frames": s.audio_head_mute_frames,
        "audio_head_mute_sec": audio_head_mute_sec,
```

在 `stream.py` 的 shot 快照 dict 里同样加（内联表达式）：

```python
                        "audio_head_mute_frames": s.audio_head_mute_frames,
                        "audio_head_mute_sec": (
                            s.audio_head_mute_frames / s.source_fps
                            if (s.audio_head_mute_frames and s.source_fps) else None
                        ),
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `uv run --project . pytest tests/integration/test_audio_head_mute.py -q` → 2 passed
Run: `uv run --project . pytest tests/ -q` → 与基线一致（仅 3 既有失败）

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/project.py backend/app/db.py backend/app/models/schemas.py backend/app/api/projects.py backend/app/api/stream.py backend/tests/integration/test_audio_head_mute.py
git commit -m "feat(audio-mute): Shot.audio_head_mute_frames EDL field + serialization"
```

---

### Task 2: `build_effective_clip` 前段静音滤镜

**Files:**
- Modify: `backend/app/agents/effective_clip.py`（`build_effective_clip` 签名 + af 组装 ~19-67；`effective_clip_paths` ~99-108）
- Test: `backend/tests/unit/test_effective_clip_head_mute.py`（新建）

**Interfaces:**
- Consumes: `Shot.audio_head_mute_frames`（Task 1）。
- Produces: `build_effective_clip(..., audio_head_mute_frames: int | None = None)`；`effective_clip_paths` 透传 `s.audio_head_mute_frames`（Task 无下游，导出/预览用）。

- [ ] **Step 1: 写失败测试**

```python
"""build_effective_clip: 前段静音把开头压到 ~0，越过点后保留原声。"""
import shutil
import subprocess
import pytest
from pathlib import Path
from ffmpeg import FFmpeg
from app.agents.effective_clip import build_effective_clip

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not found")


def _make_av(path: Path, total: float = 2.0):
    (FFmpeg().option("y")
     .input(f"color=blue:size=64x64:rate=24:duration={total}", f="lavfi")
     .input(f"sine=frequency=440:duration={total}", f="lavfi")
     .output(str(path), pix_fmt="yuv420p", vcodec="libx264", acodec="aac", shortest=None)
    ).execute()


def _rms_db(path: str, start: float, end: float) -> float:
    """astats mean RMS (dB) over [start,end]."""
    r = subprocess.run(
        ["ffmpeg", "-ss", str(start), "-to", str(end), "-i", path,
         "-af", "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    import re
    vals = [float(m.group(1)) for m in re.finditer(r"RMS_level=(-?[\d.]+|-inf)".replace("|-inf",""), r.stderr)] \
        or [float(m.group(1)) for m in re.finditer(r"RMS_level=(-?[\d.]+)", r.stderr)]
    return min(vals) if vals else 0.0


def test_head_mute_silences_front_keeps_rest(tmp_path):
    src = tmp_path / "src.mp4"; _make_av(src, 2.0)
    out = tmp_path / "out.mp4"
    # 24fps，静音前 24 帧 = 前 1.0s
    build_effective_clip(str(src), trim_frames=None, vc_audio_path=None,
                         out_path=str(out), audio_head_mute_frames=24)
    assert out.exists()
    front = _rms_db(str(out), 0.1, 0.8)   # 应接近静音（很低 dB）
    back = _rms_db(str(out), 1.2, 1.8)    # 应有声（相对高）
    assert front < back - 20, f"front={front} back={back}: 前段未被静音"


def test_no_head_mute_is_passthrough_copy(tmp_path):
    src = tmp_path / "src.mp4"; _make_av(src, 1.0)
    out = tmp_path / "out.mp4"
    build_effective_clip(str(src), trim_frames=None, vc_audio_path=None,
                         out_path=str(out), audio_head_mute_frames=None)
    assert out.exists()  # 无任何编辑 → 直接 copy
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run --project . pytest tests/unit/test_effective_clip_head_mute.py -q`
Expected: FAIL — `build_effective_clip` 无 `audio_head_mute_frames` 参数（TypeError）。

- [ ] **Step 3: 实现**

`effective_clip.py` 改 `build_effective_clip`：

签名加参数（放在 `out_path` 之后 keyword 段）：

```python
def build_effective_clip(
    source_path: str,
    *,
    trim_frames: int | None,
    vc_audio_path: str | None,
    out_path: str,
    audio_head_mute_frames: int | None = None,
    vcodec: str = "libx264",
    crf: int = 18,
    acodec: str = "aac",
) -> None:
```

把开头的 no-edit 短路条件加上 head-mute：

```python
    if not trim_frames and not vc_audio_path and not audio_head_mute_frames:
        shutil.copy2(source_path, out_path)
        return
```

在 `opts` 组装处，统一处理 af 链（替换现有 `if trim_frames: ... elif vc_audio_path:` 段为下面的组合逻辑）：

```python
    fps = None
    af_parts: list[str] = []
    if trim_frames:
        from app.agents.video_trimmer import get_video_info
        fps = get_video_info(source_path)["fps"]
        opts["vframes"] = trim_frames
        af_parts.append(f"atrim=end={trim_frames / fps:.6f}")
        af_parts.append("asetpts=PTS-STARTPTS")
    elif vc_audio_path:
        opts["shortest"] = None
    if audio_head_mute_frames:
        if fps is None:
            from app.agents.video_trimmer import get_video_info
            fps = get_video_info(source_path)["fps"]
        mute_sec = audio_head_mute_frames / fps
        # 只把 t < mute_sec 的音频压到 0，其余原样、时间轴不动 → A/V 不错位
        af_parts.append(f"volume=enable='lt(t\\,{mute_sec:.6f})':volume=0")
    if af_parts:
        opts["af"] = ",".join(af_parts)
```

（注意：`volume` 的 enable 表达式里逗号需转义为 `\\,`，否则被 ffmpeg 当滤镜分隔。）

更新末尾 log：

```python
    logger.info(
        "Effective clip %s (trim=%s vc=%s headmute=%s)",
        out_path, trim_frames, bool(vc_audio_path), audio_head_mute_frames,
    )
```

`effective_clip_paths` 里：no-edit 短路加 head-mute，且透传字段。把：

```python
        if not s.trim_frames and not vc_audio:
            out.append(str(source))
            continue
```

改为：

```python
        head_mute = getattr(s, "audio_head_mute_frames", None)
        if not s.trim_frames and not vc_audio and not head_mute:
            out.append(str(source))
            continue
```

并给 `build_effective_clip(...)` 调用加 `audio_head_mute_frames=head_mute,`。

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `uv run --project . pytest tests/unit/test_effective_clip_head_mute.py -q` → 2 passed
Run: `uv run --project . pytest tests/ -q` → 仅 3 既有失败

- [ ] **Step 5: Commit**

```bash
git add backend/app/agents/effective_clip.py backend/tests/unit/test_effective_clip_head_mute.py
git commit -m "feat(audio-mute): head-mute volume filter in build_effective_clip (orthogonal to trim/vc)"
```

---

### Task 3: `detect_speech_start` + 两个端点

**Files:**
- Modify: `backend/app/agents/video_trimmer.py`（加 `detect_speech_start`；参考 `detect_speech_end` ~20）
- Modify: `backend/app/api/pipeline.py`（加 `detect-speech-start` 端点 ~detect-silence 附近 1818；加 `audio-head-mute` PUT 端点）
- Test: `backend/tests/unit/test_speech_start.py`、`backend/tests/integration/test_audio_head_mute.py`（追加端点测试）

**Interfaces:**
- Consumes: `Shot.audio_head_mute_frames`（Task 1）、`_dialog_source`（pipeline.py 已有）。
- Produces: `detect_speech_start(video_path) -> float | None`；`POST detect-speech-start` → `{has_lead_silence, suggested_start_frame, ...video_info}`；`PUT audio-head-mute` 写字段。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_speech_start.py`：

```python
import shutil
import pytest
from pathlib import Path
from ffmpeg import FFmpeg
from app.agents.video_trimmer import detect_speech_start

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not found")


def _lead_silence_then_tone(path: Path, silence: float = 1.0, tone: float = 1.0):
    # 前 silence 秒静音，再 tone 秒 440Hz
    (FFmpeg().option("y")
     .input(f"anullsrc=r=44100:cl=mono:d={silence}", f="lavfi")
     .input(f"sine=frequency=440:duration={tone}", f="lavfi")
     .option("filter_complex", "[0][1]concat=n=2:v=0:a=1[a]")
     .output(str(path), map="[a]", acodec="pcm_s16le")
    ).execute()


def test_detect_speech_start_finds_lead_silence(tmp_path):
    p = tmp_path / "a.wav"; _lead_silence_then_tone(p, 1.0, 1.0)
    start = detect_speech_start(str(p))
    assert start is not None
    assert 0.7 < start < 1.3, f"起始应≈1.0s，实际 {start}"


def test_detect_speech_start_none_when_no_lead_silence(tmp_path):
    p = tmp_path / "b.wav"
    (FFmpeg().option("y").input("sine=frequency=440:duration=1.0", f="lavfi")
     .output(str(p), acodec="pcm_s16le")).execute()
    assert detect_speech_start(str(p)) in (None, 0.0) or detect_speech_start(str(p)) < 0.2
```

端点测试追加到 `tests/integration/test_audio_head_mute.py`：

```python
async def test_put_audio_head_mute_persists(client, db_session_factory):
    from tests.integration.conftest import HEADERS, _make_project
    from sqlalchemy import select
    from app.models.project import Shot
    pid = await _make_project(db_session_factory, status="shot_review")
    async with db_session_factory() as s:
        s.add(Shot(project_id=pid, shot_id=1, text="t", shot_type="Medium Shot",
                   visual_description="v", shot_duration=6, status="completed",
                   align_with_previous=False, source_fps=24.0))
        await s.commit()

    r = await client.put(f"/api/projects/{pid}/shots/1/audio-head-mute",
                         json={"head_mute_frames": 18}, headers=HEADERS)
    assert r.status_code == 200
    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id==pid, Shot.shot_id==1))).scalar_one()
        assert shot.audio_head_mute_frames == 18

    # 0 = 清除 → None
    r = await client.put(f"/api/projects/{pid}/shots/1/audio-head-mute",
                         json={"head_mute_frames": 0}, headers=HEADERS)
    assert r.status_code == 200
    async with db_session_factory() as s:
        shot = (await s.execute(select(Shot).where(Shot.project_id==pid, Shot.shot_id==1))).scalar_one()
        assert shot.audio_head_mute_frames is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run --project . pytest tests/unit/test_speech_start.py tests/integration/test_audio_head_mute.py::test_put_audio_head_mute_persists -q`
Expected: FAIL — `detect_speech_start` 不存在 / PUT 404。

- [ ] **Step 3: 实现**

`video_trimmer.py` 加（放在 `detect_speech_end` 之后）：

```python
def detect_speech_start(
    video_path: str,
    silence_threshold_db: float = -30,
    min_silence_duration: float = 0.3,
) -> float | None:
    """检测开头静音结束（语音起始）的时间戳（秒）。

    用 ffmpeg silencedetect 找从 0 开始的 LEADING 静音段；返回其 silence_end
    （= 语音开始）。没有开头静音 → None。
    """
    result = subprocess.run(
        ["ffmpeg", "-i", video_path,
         "-af", f"silencedetect=noise={silence_threshold_db}dB:d={min_silence_duration}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    output = result.stderr
    starts = [float(m.group(1)) for m in re.finditer(r"silence_start:\s*(-?[\d.]+)", output)]
    ends = [float(m.group(1)) for m in re.finditer(r"silence_end:\s*([\d.]+)", output)]
    # leading 静音：第一个 silence_start 在 ~0 处，取其配对的 silence_end
    if starts and ends and starts[0] <= 0.05 and ends[0] > starts[0]:
        return ends[0]
    return None
```

（`re` 和 `subprocess` 该模块已 import，复用。）

`pipeline.py` 在 `detect-silence` 端点后加两个端点：

```python
@router.post("/projects/{project_id}/shots/{shot_id}/detect-speech-start")
async def detect_speech_start_ep(
    project_id: str,
    shot_id: int,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """从开头静音推断语音起始帧 — 只读，不写文件。"""
    from app.agents.video_trimmer import detect_speech_start, get_video_info

    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot or not shot.video_path:
        raise HTTPException(status_code=404, detail="Shot or video not found")

    source = _dialog_source(project_id, shot_id, shot.video_path)
    info = get_video_info(source)
    start_sec = detect_speech_start(source)
    if start_sec is None:
        return {"has_lead_silence": False, "suggested_start_frame": None, **info,
                "source_video_url": to_media_url(source)}
    return {"has_lead_silence": True,
            "suggested_start_frame": int(round(start_sec * info["fps"])),
            "speech_start_sec": start_sec, **info,
            "source_video_url": to_media_url(source)}


@router.put("/projects/{project_id}/shots/{shot_id}/audio-head-mute")
async def set_audio_head_mute(
    project_id: str,
    shot_id: int,
    body: dict,
    user: str = Depends(_require_user),
    session: AsyncSession = Depends(get_session),
):
    """写前段静音帧数（0 = 清除）。纯 EDL，不动素材/trim/vc。"""
    await _get_project_or_404(project_id, session)
    result = await session.execute(
        select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404, detail="Shot not found")
    n = int(body.get("head_mute_frames") or 0)
    shot.audio_head_mute_frames = n if n > 0 else None
    session.add(shot)
    await session.commit()
    sec = (shot.audio_head_mute_frames / shot.source_fps) if (shot.audio_head_mute_frames and shot.source_fps) else None
    return {"shot_id": shot_id, "audio_head_mute_frames": shot.audio_head_mute_frames,
            "audio_head_mute_sec": sec}
```

（`to_media_url` 已在 pipeline.py 顶部 import。）

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `uv run --project . pytest tests/unit/test_speech_start.py tests/integration/test_audio_head_mute.py -q` → 全 passed
Run: `uv run --project . pytest tests/ -q` → 仅 3 既有失败

- [ ] **Step 5: Commit**

```bash
git add backend/app/agents/video_trimmer.py backend/app/api/pipeline.py backend/tests/unit/test_speech_start.py backend/tests/integration/test_audio_head_mute.py
git commit -m "feat(audio-mute): detect-speech-start + audio-head-mute endpoints"
```

---

### Task 4: 前端类型 + api + 播放静音

**Files:**
- Modify: `frontend-vite/src/lib/types.ts`（Shot 加两字段）
- Modify: `frontend-vite/src/lib/api.ts`（`detectSpeechStart`、`setAudioHeadMute`）
- Modify: `frontend-vite/src/components/ShotPlayer.tsx`（prop `headMuteSec` + 静音逻辑）
- Modify: `frontend-vite/src/hooks/useShotSync.ts`（onTimeUpdate 里按 headMuteSec 静音生效音轨）
- Modify: `frontend-vite/src/components/ShotCard.tsx`（ShotPlayer 传 `headMuteSec`，L716-719 附近）
- Test: `frontend-vite/src/components/__tests__/ShotPlayer.headmute.test.tsx`（新建）

**Interfaces:**
- Consumes: `audio_head_mute_sec`（Task 1）、端点（Task 3）。
- Produces: `ShotPlayer` prop `headMuteSec: number | null`；`useShotSync({ trimEndSec, audioEnabled, headMuteSec })`。

- [ ] **Step 1: 写失败测试**

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import { ShotPlayer } from '../ShotPlayer'

// jsdom video/audio 元素 currentTime 可写；模拟 timeupdate 时的静音切换
describe('ShotPlayer 前段静音', () => {
  it('currentTime < headMuteSec 时音轨静音，越过后取消', () => {
    const { container } = render(
      <ShotPlayer videoUrl="/v.mp4" trimEndSec={null} audioUrl="/a.wav" headMuteSec={1.0} />
    )
    const video = container.querySelector('video') as HTMLVideoElement
    const audio = container.querySelector('audio') as HTMLAudioElement
    // 位于静音区
    Object.defineProperty(video, 'currentTime', { value: 0.5, configurable: true })
    video.dispatchEvent(new Event('timeupdate'))
    expect(audio.muted).toBe(true)
    // 越过静音区
    Object.defineProperty(video, 'currentTime', { value: 1.5, configurable: true })
    video.dispatchEvent(new Event('timeupdate'))
    expect(audio.muted).toBe(false)
  })

  it('headMuteSec=null 时不干预静音', () => {
    const { container } = render(
      <ShotPlayer videoUrl="/v.mp4" trimEndSec={null} audioUrl="/a.wav" headMuteSec={null} />
    )
    const video = container.querySelector('video') as HTMLVideoElement
    const audio = container.querySelector('audio') as HTMLAudioElement
    Object.defineProperty(video, 'currentTime', { value: 0.1, configurable: true })
    video.dispatchEvent(new Event('timeupdate'))
    expect(audio.muted).toBe(false)
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/ShotPlayer.headmute.test.tsx`
Expected: FAIL — ShotPlayer 无 `headMuteSec` prop，静音不切换。

- [ ] **Step 3: 实现**

`types.ts` 的 `Shot` 接口（`trim_end_sec` 附近）加：

```ts
  audio_head_mute_frames?: number | null
  audio_head_mute_sec?: number | null
```

`api.ts` 加（`detectSilence` 附近）：

```ts
  detectSpeechStart: (projectId: string, shotId: number): Promise<{
    has_lead_silence: boolean; suggested_start_frame: number | null
    fps: number; total_frames: number; duration: number; source_video_url?: string | null
  }> => request('POST', `/api/projects/${projectId}/shots/${shotId}/detect-speech-start`),

  setAudioHeadMute: (projectId: string, shotId: number, headMuteFrames: number): Promise<{
    shot_id: number; audio_head_mute_frames: number | null; audio_head_mute_sec: number | null
  }> => request('PUT', `/api/projects/${projectId}/shots/${shotId}/audio-head-mute`, { head_mute_frames: headMuteFrames }),
```

`useShotSync.ts` 改签名与 onTimeUpdate。接口加 `headMuteSec`：

```ts
export interface ShotSyncOptions {
  trimEndSec: number | null
  audioEnabled: boolean
  headMuteSec: number | null
}
```

在 `useShotSync({ trimEndSec, audioEnabled, headMuteSec })` 解构，并在 `onTimeUpdate` 里（clamp 之后）加静音切换。因为生效音轨可能是 `<audio>`（vc）或 `<video>`（原音），暴露一个判断：

```ts
  const onTimeUpdate = useCallback(() => {
    const v = videoRef.current
    const a = audioRef.current
    if (!v) return
    if (trimEndSec != null && v.currentTime >= trimEndSec) {
      v?.pause?.(); a?.pause?.(); return
    }
    if (audioEnabled && a && Math.abs(a.currentTime - v.currentTime) > DRIFT_TOLERANCE) {
      a.currentTime = v.currentTime
    }
    if (headMuteSec != null) {
      const inMute = v.currentTime < headMuteSec
      // 生效音轨：有 vc 用 audio，否则视频自带音轨
      if (audioEnabled && a) a.muted = inMute
      else v.muted = inMute
    }
  }, [trimEndSec, audioEnabled, headMuteSec])
```

注意：ShotPlayer 里 `<video muted={audioEnabled}>` 已在有 vc 时静音视频原音；无 vc 时 `v.muted` 由本逻辑接管。为避免冲突，ShotPlayer 的 video `muted` 改为受控表达式（见下）。

`ShotPlayer.tsx`：props 接口加 `headMuteSec: number | null`；传给 `useShotSync`：

```tsx
export interface ShotPlayerProps {
  videoUrl: string
  trimEndSec: number | null
  audioUrl: string | null
  headMuteSec: number | null
  poster?: string | null
}
```

```tsx
export function ShotPlayer({ videoUrl, trimEndSec, audioUrl, headMuteSec, poster }: ShotPlayerProps) {
  ...
  const { videoRef, audioRef, onPlay, onPause, onSeeked, onTimeUpdate } =
    useShotSync({ trimEndSec, audioEnabled, headMuteSec })
```

video 的 `muted` 保持 `muted={audioEnabled}`（有 vc 时视频恒静音，vc 音轨由 onTimeUpdate 控制；无 vc 时 audioEnabled=false，视频原音由 onTimeUpdate 的 `v.muted` 控制——两者不冲突，因为 audioEnabled=false 时 useShotSync 只碰 v.muted）。

`ShotCard.tsx` L716-719 的 ShotPlayer 调用加一行：

```tsx
                headMuteSec={shot.audio_head_mute_sec ?? null}
```

- [ ] **Step 4: 跑测试确认通过 + 组件全量**

Run: `npx vitest run src/components/__tests__/ShotPlayer.headmute.test.tsx` → 全 PASS
Run: `npx vitest run` → 既有全过 + 本次新增

- [ ] **Step 5: Commit**

```bash
git add frontend-vite/src/lib/types.ts frontend-vite/src/lib/api.ts frontend-vite/src/components/ShotPlayer.tsx frontend-vite/src/hooks/useShotSync.ts frontend-vite/src/components/ShotCard.tsx frontend-vite/src/components/__tests__/ShotPlayer.headmute.test.tsx
git commit -m "feat(audio-mute): ShotPlayer/useShotSync mute effective audio during head-mute region"
```

---

### Task 5: 波形前手柄 + TrimDialog 集成

**Files:**
- Modify: `frontend-vite/src/components/WaveformTrack.tsx`（前手柄/蓝线/淡蓝遮罩 + 图例；props 加 `headMuteFrame` + `onHeadMuteScrub`）
- Modify: `frontend-vite/src/components/TrimDialog.tsx`（state `headMuteFrame`、载入、检测按钮、帧信息、保存、WaveformTrack 接线）
- Test: `frontend-vite/src/components/__tests__/WaveformTrack.test.tsx`（追加）、`TrimDialog.test.tsx`（追加）

**Interfaces:**
- Consumes: `api.detectSpeechStart`/`api.setAudioHeadMute`（Task 4）、`audio_head_mute_frames`（Task 1）。
- Produces: 无下游。

- [ ] **Step 1: 写失败测试**（WaveformTrack.test.tsx 追加）

```tsx
  it('headMuteFrame>0 时画蓝色前手柄线 + 左侧淡蓝遮罩', () => {
    render(
      <WaveformTrack peaks={samplePeaks} totalFrames={240} endFrame={240}
        speechEndFrame={null} headMuteFrame={30} onScrub={() => {}} onHeadMuteScrub={() => {}} />,
    )
    // 蓝色前手柄线 #3B82F6 与淡蓝遮罩 rgba(59, 130, 246, 0.14) 均被绘制
    expect(fillStyleLog).toContain('#2563EB')          // 前手柄竖线 blue-600
    expect(fillStyleLog).toContain('rgba(37, 99, 235, 0.14)')  // 左侧遮罩
  })
```

（TrimDialog.test.tsx 追加：mock `api.detectSpeechStart` 返回 `suggested_start_frame`，断言点“检测开头静音”后帧信息出现“前段静音”。因 TrimDialog mock 结构较大，实现时按文件现有 `vi.mock('@/lib/api')` 追加这两个函数的 mock，并复用现有 renderDialog。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/WaveformTrack.test.tsx`
Expected: 新增 FAIL — WaveformTrack 无 `headMuteFrame` 绘制。

- [ ] **Step 3: 实现**

`WaveformTrack.tsx` props 接口加：

```ts
  headMuteFrame?: number
  onHeadMuteScrub?: (frame: number) => void
```

在 canvas 绘制里（峰值柱之后、裁剪竖线之前）加前段遮罩 + 手柄线：

```ts
    // 前段静音：左侧淡蓝遮罩 + 蓝色手柄线
    if (headMuteFrame && headMuteFrame > 0) {
      const hx = pixelForFrame(headMuteFrame, width, totalFrames)
      g.fillStyle = 'rgba(37, 99, 235, 0.14)' // blue-600 淡
      g.fillRect(0, 0, hx, TRACK_HEIGHT)
      g.fillStyle = '#2563EB' // blue-600
      g.fillRect(hx - 1, 0, 2, TRACK_HEIGHT)
    }
```

图例 span 文案追加 ` · 蓝前段=静音`（放在现有图例字符串末尾）。绘制 effect 的依赖数组加 `headMuteFrame`。

交互：现有 `scrubTo` 用于设裁剪点（尾）。前手柄需要一个独立的拖拽区——最小实现：在波形左侧 ~20% 区域内的 pointerdown 走 `onHeadMuteScrub`，其余走 `onScrub`。实现时在 `scrubTo` 里按 offsetX 判断：若 `onHeadMuteScrub` 存在且点击落在 `headMuteFrame` 手柄附近（±10px）或左 15% 区，调用 `onHeadMuteScrub(frameFromOffsetX(...))`，否则 `onScrub`。（保持 TDD：先渲染断言，交互细节在实现步内完成，e2e 覆盖端到端。）

`TrimDialog.tsx`：
- state：`const [headMuteFrame, setHeadMuteFrame] = useState(0)`。
- 载入 effect 里：`setHeadMuteFrame(shot.audio_head_mute_frames ?? 0)`。
- WaveformTrack 传 `headMuteFrame={headMuteFrame}` `onHeadMuteScrub={(f) => setHeadMuteFrame(Math.max(0, Math.min(f, totalFrames)))}`。
- 加“自动检测开头静音”按钮，onClick：

```tsx
  const handleDetectSpeechStart = async () => {
    try {
      const r = await api.detectSpeechStart(projectId, shot.shot_id)
      if (r.has_lead_silence && r.suggested_start_frame != null) setHeadMuteFrame(r.suggested_start_frame)
      else setNotice('未检测到开头静音')
    } catch (e) { setError(e instanceof Error ? e.message : '检测失败') }
  }
```

- 帧信息行加（headMuteFrame>0 时）：

```tsx
                {headMuteFrame > 0 && (
                  <span className="text-blue-600 ml-2">
                    前段静音: 前 {headMuteFrame} 帧 / {(headMuteFrame / fps).toFixed(2)}s
                  </span>
                )}
```

- 保存：在现有确认/保存链里，若 `headMuteFrame` 与初始不同则调 `await api.setAudioHeadMute(projectId, shot.shot_id, headMuteFrame)`，并 `onShotUpdated?.(shot.shot_id, { audio_head_mute_frames: headMuteFrame || null, audio_head_mute_sec: fps ? (headMuteFrame/fps) || null : null })`（若无 onShotUpdated 回调则触发父层刷新）。实现时挂在“确认裁剪”成功后或单独一个“应用”动作——与 trim 正交，独立 PUT，不阻塞 trim。

- [ ] **Step 4: 跑测试确认通过 + 全量**

Run: `npx vitest run` → 全过（既有 + 新增）
Run: `npx tsc -b --pretty false 2>&1 | grep -c "error TS"` → 与基线相同数量（不新增）

- [ ] **Step 5: Commit**

```bash
git add frontend-vite/src/components/WaveformTrack.tsx frontend-vite/src/components/TrimDialog.tsx frontend-vite/src/components/__tests__/WaveformTrack.test.tsx frontend-vite/src/components/__tests__/TrimDialog.test.tsx
git commit -m "feat(audio-mute): waveform front handle + detect-speech-start + save in TrimDialog"
```

---

### Task 6: e2e + 部署验证 + PR

**Files:**
- Modify: `frontend-vite/e2e/waveform-trim.spec.ts`（追加前段静音用例）

- [ ] **Step 1: e2e 用例**

在既有 hermetic spec 追加（mock `detect-speech-start`、`audio-head-mute`；mock shot 加 `audio_head_mute_frames`/`audio_head_mute_sec`）：

```ts
  test('前段静音：波形前手柄 + 帧信息', async ({ page }) => {
    await page.route('**/api/projects/*/shots/*/detect-speech-start', (route) =>
      route.fulfill({ json: { has_lead_silence: true, suggested_start_frame: 24, fps: 24, total_frames: 117, duration: 4.875 } }))
    await page.route('**/api/projects/*/shots/*/audio-head-mute', (route) =>
      route.fulfill({ json: { shot_id: 1, audio_head_mute_frames: 24, audio_head_mute_sec: 1.0 } }))
    await page.goto(`/projects/${PROJECT_ID}/shots`)
    await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 10_000 })
    await page.getByRole('button', { name: '裁剪' }).first().click()
    await expect(page.getByText('裁剪视频 — Shot #1')).toBeVisible({ timeout: 5_000 })
    await page.getByRole('button', { name: /检测开头静音/ }).click()
    await expect(page.getByText(/前段静音:\s*前\s*24\s*帧/)).toBeVisible()
  })
```

- [ ] **Step 2: 跑 e2e**

前置：stack 运行中（4000/8002）。
Run: `npx playwright test e2e/waveform-trim.spec.ts`
Expected: 既有 + 新用例全 PASS。

- [ ] **Step 3: 部署验证**

```bash
podman restart video-maker-backend-dev video-maker-worker-dev   # 先确认无在跑任务
curl -s localhost:8002/openapi.json | grep -o "audio-head-mute"
```

用真实已生成 shot：裁剪弹窗设前段静音 + 检测 + 播放，确认静音区无声、越过有声。**不触发任何生成。**

- [ ] **Step 4: Commit + push + draft PR**

```bash
git add frontend-vite/e2e/waveform-trim.spec.ts
git commit -m "test(e2e): audio head-mute front handle + detect"
git push
# PR 已存在（本分支），无需新建；若需单独 PR 再议
```
