# 连贯性预览音频格式不匹配 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复连贯性预览在音色校准分镜处冻结的问题——统一 effective clip 的音频格式，并把三个拼接函数合并为一个基于 concat 滤镜的 `merge_shots()`。

**Architecture:** 两层防御。第一层让 `build_effective_clip()` 把所有重编码片段固定为 48 kHz 立体声，从源头消除格式差异；第二层让 `merge_shots()` 改用 ffmpeg concat **滤镜**（为每个输入独立建解码器并自动重采样）取代 concat **demuxer**（只配置一次解码器，后续片段被喂给错误的解码器）。顺带移除 crossfade，导出改为硬切，预览与导出走同一条代码路径。

**Tech Stack:** Python 3.12、FastAPI、ARQ worker、`python-ffmpeg`(`from ffmpeg import FFmpeg`)、pytest / pytest-asyncio、ffmpeg CLI。

## Global Constraints

- 规范音频格式：`CANONICAL_SAMPLE_RATE = 48000`、`CANONICAL_CHANNELS = 2`。所有重编码产物必须是这个格式。
- 所有 Python 通过 `uv` 运行；**禁止**直接调 `python` / `python3` / `pip install`。测试命令一律在**仓库根目录**执行：`uv run --project backend pytest ...`。
- **禁止硬编码绝对路径**。测试里用 `tmp_path`；代码里用 `pathlib` 相对 `__file__` 解析。
- `-pix_fmt yuv420p` 必须保留在 libx264 输出上：浏览器与多数硬件解码器无法解码 `High 4:4:4 Predictive`。
- 单片段合并必须保持 `c=copy`（字节保真）——`test_merger_effective.py` 依赖它做末帧 md5 校验。
- 每次 commit 前跑该任务对应的测试，绿了再提交。
- 参考 spec：`docs/superpowers/specs/2026-07-10-join-preview-audio-mismatch-design.md`

---

## File Structure

| 文件 | 职责 | 本计划中的改动 |
|------|------|----------------|
| `backend/app/agents/merger.py` | 把若干 effective clip 拼成一条视频 | **重写**：三函数合一，concat 滤镜，导出 `CANONICAL_*` 常量 |
| `backend/app/agents/effective_clip.py` | 由源片 + EDL 元数据烤出 effective clip | 重编码时固定 `ar/ac` |
| `backend/worker/tasks.py` | ARQ `run_merger` 导出任务 | 去掉 `crossfade_duration` 参数 |
| `backend/app/api/pipeline.py` | `/export`、`/join-preview` 端点 | 导出端点去掉 body；enqueue 去掉第三个参数 |
| `backend/app/models/schemas.py` | 请求体 schema | 删除 `ExportRequest` |
| `backend/app/config.py` | 全局设置 | 删除 `crossfade_duration` |
| `backend/tests/unit/test_ffmpeg_agents.py` | merger 单测 | 删掉两个旧类；`TestMergeShots` 加音频 fixture、加回归/守卫测试 |
| `backend/tests/unit/test_effective_clip_audio.py` | **新建** | 断言重编码片段是 48 kHz 立体声 |
| `backend/tests/unit/test_merger_effective.py` | 末帧 md5 不变性 | 改调 `merge_shots` |
| `backend/tests/integration/test_join_preview_vc_audio.py` | **新建** | 真实 `/join-preview` 端点：trim + VC 混合，断言音视频时长对齐、零解码错误 |

依赖方向：`effective_clip.py` → `merger.py`（导入常量）。`merger.py` 不 import `effective_clip.py`，无循环。

---

### Task 1: effective_clip 固定输出为规范音频格式

**Files:**
- Create: `backend/tests/unit/test_effective_clip_audio.py`
- Modify: `backend/app/agents/merger.py`（仅新增常量，函数暂不动）
- Modify: `backend/app/agents/effective_clip.py:49`

**Interfaces:**
- Produces: `merger.CANONICAL_SAMPLE_RATE: int = 48000`、`merger.CANONICAL_CHANNELS: int = 2`
- Produces: `build_effective_clip(...)` 重编码路径的输出音频恒为 48 kHz 立体声

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/unit/test_effective_clip_audio.py`：

```python
"""Regression: a voice-cloned effective clip must be baked at the canonical
audio format (48 kHz stereo), not at whatever rate/layout the VC wav happens
to have.  A 24 kHz mono clip cannot be concat-copied with a 48 kHz stereo one.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from app.agents.effective_clip import build_effective_clip
from app.agents.merger import CANONICAL_CHANNELS, CANONICAL_SAMPLE_RATE

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg binary not found in PATH",
)


def _audio_params(path: Path) -> tuple[int, int]:
    """Return (sample_rate, channels) of the first audio stream."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels",
            "-of", "csv=p=0", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    rate, channels = out.stdout.strip().split(",")
    return int(rate), int(channels)


def _make_source(path: Path, frames: int = 60) -> None:
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440",
         "-frames:v", str(frames),
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac",
         "-shortest", str(path)],
        check=True, capture_output=True,
    )


def _make_vc_wav(path: Path, seconds: float = 3.0) -> None:
    """A CosyVoice-shaped wav: 24 kHz, mono."""
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=330:sample_rate=24000:duration={seconds}",
         "-ac", "1", "-c:a", "pcm_s16le", str(path)],
        check=True, capture_output=True,
    )


def test_vc_clip_is_normalized_to_canonical_audio(tmp_path):
    src = tmp_path / "output.mp4"
    wav = tmp_path / "audio_vc.wav"
    out = tmp_path / "eff.mp4"
    _make_source(src)
    _make_vc_wav(wav)

    build_effective_clip(str(src), trim_frames=None, vc_audio_path=str(wav),
                         out_path=str(out))

    assert _audio_params(out) == (CANONICAL_SAMPLE_RATE, CANONICAL_CHANNELS)


def test_trimmed_clip_is_normalized_to_canonical_audio(tmp_path):
    src = tmp_path / "output.mp4"
    out = tmp_path / "eff.mp4"
    _make_source(src)

    build_effective_clip(str(src), trim_frames=30, vc_audio_path=None,
                         out_path=str(out))

    assert _audio_params(out) == (CANONICAL_SAMPLE_RATE, CANONICAL_CHANNELS)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest backend/tests/unit/test_effective_clip_audio.py -q`

Expected: FAIL —— 收集阶段就报 `ImportError: cannot import name 'CANONICAL_CHANNELS' from 'app.agents.merger'`（常量还不存在）。

- [ ] **Step 3: 在 merger.py 顶部加常量**

在 `backend/app/agents/merger.py` 的 `logger = logging.getLogger(__name__)` 之后插入：

```python
# The canonical audio format for every re-encoded clip and every merged video.
# ffmpeg's concat demuxer writes ONE audio decoder config for all segments, so
# clips whose rate/layout differ decode as garbage.  Pinning both ends here is
# what keeps that from happening.
CANONICAL_SAMPLE_RATE = 48000
CANONICAL_CHANNELS = 2
```

- [ ] **Step 4: 在 effective_clip.py 里应用常量**

在 `backend/app/agents/effective_clip.py` 的 import 区加：

```python
from app.agents.merger import CANONICAL_CHANNELS, CANONICAL_SAMPLE_RATE
```

把 `build_effective_clip` 里这一行（原 49 行）：

```python
    opts: dict = {"map": ["0:v", audio_map], "vcodec": vcodec, "acodec": acodec}
```

替换为：

```python
    opts: dict = {
        "map": ["0:v", audio_map],
        "vcodec": vcodec,
        "acodec": acodec,
        # Without this a VC clip inherits the CosyVoice wav's 24 kHz mono layout.
        "ar": CANONICAL_SAMPLE_RATE,
        "ac": CANONICAL_CHANNELS,
    }
```

同时更新该函数的 docstring，在 `- No edits → straight copy of the source bytes.` 之前插入一行：

```python
    - Re-encoded output is always CANONICAL_SAMPLE_RATE / CANONICAL_CHANNELS audio.
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run --project backend pytest backend/tests/unit/test_effective_clip_audio.py -q`

Expected: PASS，2 passed。

- [ ] **Step 6: 确认没打破末帧 md5 不变性**

Run: `uv run --project backend pytest backend/tests/unit/test_merger_effective.py -q`

Expected: PASS，1 passed。（该测试用 `ffv1`/`pcm_s16le` 无损烤片；`ar`/`ac` 只影响音频轨，它断言的是视频帧 md5。）

- [ ] **Step 7: Commit**

```bash
git add backend/app/agents/merger.py backend/app/agents/effective_clip.py backend/tests/unit/test_effective_clip_audio.py
git commit -m "fix(effective_clip): 重编码片段固定为 48kHz 立体声

VC 片段此前继承 CosyVoice wav 的 24k mono，与 trim 片段的 48k stereo
在 concat 时冲突。"
```

---

### Task 2: merge_shots 改用 concat 滤镜

**Files:**
- Modify: `backend/app/agents/merger.py`（重写 `merge_shots`；`merge_shots_with_crossfade` / `merge_shots_with_reencoding` 本任务**保持不动**，Task 3 再删）
- Modify: `backend/tests/unit/test_ffmpeg_agents.py`（`TestMergeShots` 类）

**Interfaces:**
- Consumes: `CANONICAL_SAMPLE_RATE`、`CANONICAL_CHANNELS`（Task 1）
- Produces: `merge_shots(shot_paths: list[str], output_path: str, *, vcodec: str = "libx264", preset: str = "fast", crf: int = 18) -> None`
- Produces: `_has_audio(path: str) -> bool`

**为什么本任务不动另外两个函数：** `worker/tasks.py` 还 import 着 `merge_shots_with_crossfade`。先让 `merge_shots` 正确并绿灯，再在 Task 3 里连同调用方一起清理——这样每个 commit 都是可跑的。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/unit/test_ffmpeg_agents.py` 的 helper 区（`_pix_fmt` 之后）追加三个 helper：

```python
def _make_clip_with_audio_format(path: Path, *, duration: int, rate: int, channels: int) -> None:
    """A clip whose audio is deliberately at a given rate/layout."""
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size=64x64:rate=25:duration={duration}",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={rate}",
         "-t", str(duration),
         "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ar", str(rate), "-ac", str(channels),
         "-shortest", str(path)],
        check=True, capture_output=True,
    )


def _stream_duration(path: Path, kind: str) -> float:
    """kind is 'v' or 'a'."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", f"{kind}:0",
         "-show_entries", "stream=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def _decode_errors(path: Path) -> int:
    """Count decoder failures across a full decode pass."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True,
    )
    return out.stderr.count("Error submitting packet")
```

然后把 `TestMergeShots` 里两个多片段测试的 fixture 换成带音频的版本，并新增三个测试。替换：

```python
    def test_multiple_shots_merged(self, tmp_path):
        from app.agents.merger import merge_shots

        v1 = tmp_path / "shot1.mp4"
        v2 = tmp_path / "shot2.mp4"
        output = tmp_path / "merged.mp4"
        _make_test_video(v1)
        _make_test_video(v2)

        merge_shots([str(v1), str(v2)], str(output))

        assert output.exists()
        assert output.stat().st_size > 0

    def test_output_larger_than_single_input(self, tmp_path):
        """Merged file should be at least as large as one input."""
        from app.agents.merger import merge_shots

        v1 = tmp_path / "shot1.mp4"
        v2 = tmp_path / "shot2.mp4"
        output = tmp_path / "merged.mp4"
        _make_test_video(v1)
        _make_test_video(v2)

        merge_shots([str(v1), str(v2)], str(output))

        assert output.stat().st_size >= v1.stat().st_size
```

为：

```python
    def test_multiple_shots_merged(self, tmp_path):
        from app.agents.merger import merge_shots

        v1 = tmp_path / "shot1.mp4"
        v2 = tmp_path / "shot2.mp4"
        output = tmp_path / "merged.mp4"
        _make_test_video_with_audio(v1)
        _make_test_video_with_audio(v2)

        merge_shots([str(v1), str(v2)], str(output))

        assert output.exists()
        assert output.stat().st_size > 0

    def test_output_larger_than_single_input(self, tmp_path):
        """Merged file should be at least as large as one input."""
        from app.agents.merger import merge_shots

        v1 = tmp_path / "shot1.mp4"
        v2 = tmp_path / "shot2.mp4"
        output = tmp_path / "merged.mp4"
        _make_test_video_with_audio(v1)
        _make_test_video_with_audio(v2)

        merge_shots([str(v1), str(v2)], str(output))

        assert output.stat().st_size >= v1.stat().st_size

    def test_merged_output_is_yuv420p(self, tmp_path):
        """Browsers cannot decode High 4:4:4 Predictive."""
        from app.agents.merger import merge_shots

        v1 = tmp_path / "shot1.mp4"
        v2 = tmp_path / "shot2.mp4"
        output = tmp_path / "merged.mp4"
        _make_test_video_with_audio(v1)
        _make_test_video_with_audio(v2)

        merge_shots([str(v1), str(v2)], str(output))

        assert _pix_fmt(output) == "yuv420p"

    def test_mismatched_audio_formats_stay_in_sync(self, tmp_path):
        """THE regression: a 48 kHz stereo clip + a 24 kHz mono clip (what a
        voice-cloned shot bakes to) must concat into a playable video whose
        audio spans the whole timeline.  The concat demuxer + -c copy wrote one
        decoder config for both segments, so segment 2's audio decoded as
        garbage and the <video> element's audio clock stalled -- freezing the
        picture on segment 1's last frame."""
        from app.agents.merger import merge_shots

        v1 = tmp_path / "stereo48k.mp4"
        v2 = tmp_path / "mono24k.mp4"
        output = tmp_path / "merged.mp4"
        _make_clip_with_audio_format(v1, duration=2, rate=48000, channels=2)
        _make_clip_with_audio_format(v2, duration=2, rate=24000, channels=1)

        merge_shots([str(v1), str(v2)], str(output))

        assert _decode_errors(output) == 0
        v_dur = _stream_duration(output, "v")
        a_dur = _stream_duration(output, "a")
        assert v_dur == pytest.approx(4.0, abs=0.15), f"video {v_dur}"
        assert a_dur == pytest.approx(v_dur, abs=0.15), (
            f"audio {a_dur} does not span the video {v_dur}"
        )

    def test_raises_when_input_has_no_audio(self, tmp_path):
        """The concat filter's a=1 needs an audio stream on every input;
        fail with a clear message rather than a cryptic filtergraph error."""
        from app.agents.merger import merge_shots

        v1 = tmp_path / "shot1.mp4"
        v2 = tmp_path / "silent.mp4"
        output = tmp_path / "merged.mp4"
        _make_test_video_with_audio(v1)
        _make_test_video(v2)   # no audio track

        with pytest.raises(ValueError, match="no audio stream"):
            merge_shots([str(v1), str(v2)], str(output))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --project backend pytest backend/tests/unit/test_ffmpeg_agents.py -q -k "TestMergeShots and (mismatched or no_audio)"`

Expected: FAIL，2 failed。
- `test_mismatched_audio_formats_stay_in_sync`：`assert 43 == 0`（解码错误），音频约 2.03s 而视频 4.0s。
- `test_raises_when_input_has_no_audio`：`DID NOT RAISE ValueError`（当前 `-c copy` 不会报错）。

- [ ] **Step 3: 重写 merge_shots**

把 `backend/app/agents/merger.py` 中整个 `merge_shots` 函数（从 `def merge_shots(` 到 `filelist_path.unlink(missing_ok=True)` 为止）替换为：

```python
def _has_audio(path: str) -> bool:
    """True when the file carries at least one audio stream."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return bool(out.stdout.strip())


def merge_shots(
    shot_paths: list[str],
    output_path: str,
    *,
    vcodec: str = "libx264",
    preset: str = "fast",
    crf: int = 18,
) -> None:
    """Concatenate shot videos into one, hard-cutting between clips.

    Uses ffmpeg's concat *filter*, which builds a decoder per input and
    auto-resamples.  The concat *demuxer* configures a single decoder for every
    segment, so a clip whose audio rate/layout differs from the first one (a
    voice-cloned shot, say) decodes as garbage -- and because the <video>
    element's clock is driven by audio, playback freezes at the segment
    boundary.  The demuxer corrupts silently even when re-encoding, so the
    filter is the only safe option here.

    A single input is stream-copied, which keeps it byte-exact.

    Args:
        shot_paths: Video file paths to concatenate, in order.
        output_path: Path for the merged video.

    Raises:
        ValueError: shot_paths is empty, holds no usable path, or one of the
            inputs carries no audio stream.
        RuntimeError: ffmpeg failed.
    """
    if not shot_paths:
        raise ValueError("No shot paths provided")

    valid_paths = [p for p in shot_paths if p]
    if not valid_paths:
        raise ValueError("No valid shot paths provided")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if len(valid_paths) == 1:
        (
            FFmpeg()
            .option("y")
            .input(valid_paths[0])
            .output(output_path, c="copy")
        ).execute()
        logger.info("Copied single shot to %s", output_path)
        return

    silent = [p for p in valid_paths if not _has_audio(p)]
    if silent:
        raise ValueError(f"Cannot concat: no audio stream in {silent[0]}")

    n = len(valid_paths)
    streams = "".join(f"[{i}:v][{i}:a]" for i in range(n))
    filter_complex = f"{streams}concat=n={n}:v=1:a=1[v][a]"

    cmd = ["ffmpeg", "-y"]
    for p in valid_paths:
        cmd += ["-i", str(Path(p).resolve())]
    cmd += ["-filter_complex", filter_complex, "-map", "[v]", "-map", "[a]",
            "-c:v", vcodec]
    if vcodec == "libx264":
        cmd += ["-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]
    cmd += [
        "-c:a", "aac",
        "-ar", str(CANONICAL_SAMPLE_RATE),
        "-ac", str(CANONICAL_CHANNELS),
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("Concat merge failed: %s", result.stderr[-500:])
        raise RuntimeError(f"ffmpeg concat failed: {result.stderr[-300:]}")

    logger.info("Merged %d shots to %s", n, output_path)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --project backend pytest backend/tests/unit/test_ffmpeg_agents.py -q`

Expected: PASS，**22 passed**。（改动前是 19 = `TestExtractLastFrame` 3 + `TestExtractFrameAtTime` 3 + `TestMergeShots` 7 + `TestMergeShotsWithCrossfade` 1 + `TestMergeShotsWithReencoding` 5。本任务给 `TestMergeShots` 新增 3 个：`test_merged_output_is_yuv420p`、`test_mismatched_audio_formats_stay_in_sync`、`test_raises_when_input_has_no_audio`。）

- [ ] **Step 5: Commit**

```bash
git add backend/app/agents/merger.py backend/tests/unit/test_ffmpeg_agents.py
git commit -m "fix(merger): merge_shots 改用 concat 滤镜，修复 VC 分镜处播放冻结

concat demuxer 只写一份音频解码配置，格式不同的后续片段解码为乱码，
<video> 的音频时钟随之停摆，画面冻在段边界。滤镜为每个输入独立建
解码器并自动重采样。"
```

---

### Task 3: 移除 crossfade，导出改硬切

**Files:**
- Modify: `backend/app/agents/merger.py`（删除 `merge_shots_with_crossfade`、`merge_shots_with_reencoding`、`_get_durations`）
- Modify: `backend/worker/tasks.py:50,778,787,825-826`
- Modify: `backend/app/api/pipeline.py:25,713,742`
- Modify: `backend/app/models/schemas.py:229-230`
- Modify: `backend/app/config.py:261-262`
- Modify: `backend/tests/unit/test_ffmpeg_agents.py`（删除 `TestMergeShotsWithReencoding`、`TestMergeShotsWithCrossfade`）
- Modify: `backend/tests/unit/test_merger_effective.py`

**Interfaces:**
- Consumes: `merge_shots(...)`（Task 2）
- Produces: `run_merger(ctx: Dict[str, Any], project_id: str, actor: str) -> None`（三参数，不再有 `crossfade_duration`）
- Produces: `POST /api/projects/{id}/export` 不再接受请求体

- [ ] **Step 1: 先确认没有别的调用方**

Run:
```bash
grep -rn "crossfade\|ExportRequest\|merge_shots_with_" backend/ frontend-vite/src tests/ --include=*.py --include=*.ts --include=*.tsx | grep -v "\.venv"
```

Expected: 命中只出现在本任务列出的文件里（外加 `docs/` 下的历史文档，不动）。若 `frontend-vite/` 或 `tests/e2e/` 有命中，**停下来**先处理。

- [ ] **Step 2: 更新测试（先改测试，让它们描述目标状态）**

在 `backend/tests/unit/test_ffmpeg_agents.py` 中，**整类删除** `TestMergeShotsWithReencoding`（约 217-273 行）与 `TestMergeShotsWithCrossfade`（约 276-291 行）。yuv420p 的覆盖已由 Task 2 的 `test_merged_output_is_yuv420p` 承接。

在 `backend/tests/unit/test_merger_effective.py` 中：

把
```python
from app.agents.merger import merge_shots_with_crossfade
```
改为
```python
from app.agents.merger import merge_shots
```

把
```python
    merge_shots_with_crossfade([str(clip)], str(final), crossfade_duration=0.3)
```
改为
```python
    merge_shots([str(clip)], str(final))
```

并把文件顶部 docstring 里的
```
Note: This test exercises build_effective_clip + merge_shots_with_crossfade directly with
```
改为
```
Note: This test exercises build_effective_clip + merge_shots directly with
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run --project backend pytest backend/tests/unit/test_merger_effective.py -q`

Expected: PASS（`merge_shots` 已存在且单片段走 copy）。这一步是确认改测试没有引入回归——本任务的"失败"在下一步的 import 上。

Run: `uv run --project backend pytest backend/tests/unit/ -q`

Expected: PASS。（旧函数尚在，删除它们才会暴露 `worker/tasks.py` 的 import 错误。）

- [ ] **Step 4: 删除 merger.py 里的旧函数**

从 `backend/app/agents/merger.py` 删除三段：`_get_durations`（12-23 行）、`merge_shots_with_crossfade`（26-132 行）、`merge_shots_with_reencoding`（文件末尾）。

删除后 `merger.py` 应只剩：模块 docstring、`import logging` / `import subprocess` / `from pathlib import Path` / `from ffmpeg import FFmpeg`、`logger`、两个 `CANONICAL_*` 常量、`_has_audio`、`merge_shots`。

- [ ] **Step 5: 更新 worker/tasks.py**

第 50 行：
```python
from app.agents.merger import merge_shots, merge_shots_with_crossfade
```
→
```python
from app.agents.merger import merge_shots
```

`run_merger` 签名（约 777-781 行）：
```python
async def run_merger(
    ctx: Dict[str, Any],
    project_id: str,
    actor: str,
    crossfade_duration: float | None = None,
) -> None:
```
→
```python
async def run_merger(
    ctx: Dict[str, Any],
    project_id: str,
    actor: str,
) -> None:
```

docstring 里删掉这一行（约 787 行）：
```python
        crossfade_duration: Override for crossfade (None = use settings default)
```

调用处（约 825-826 行）：
```python
            cf = crossfade_duration if crossfade_duration is not None else settings.crossfade_duration
            merge_shots_with_crossfade(shot_paths, str(final_path), crossfade_duration=cf)
```
→
```python
            merge_shots(shot_paths, str(final_path))
```

- [ ] **Step 6: 更新 api/pipeline.py**

第 25 行 import 去掉 `ExportRequest`：
```python
    ExportRequest, JoinPreviewRequest,
```
→
```python
    JoinPreviewRequest,
```
（保留该行其余符号原样；若 `ExportRequest` 是该行唯一符号以外的内容有变化，只删这一个名字。）

导出端点签名（约 710-716 行）删掉 body 参数：
```python
async def export_project(
    project_id: str,
    body: ExportRequest = ExportRequest(),
    user: str = Depends(_require_user),
```
→
```python
async def export_project(
    project_id: str,
    user: str = Depends(_require_user),
```

enqueue（约 742 行）：
```python
    await arq.enqueue_job("run_merger", project_id, f"user:{user}", body.crossfade_duration)
```
→
```python
    await arq.enqueue_job("run_merger", project_id, f"user:{user}")
```

- [ ] **Step 7: 更新 schemas.py 与 config.py**

`backend/app/models/schemas.py` 删除（229-230 行及其前后空行）：
```python
class ExportRequest(BaseModel):
    crossfade_duration: Optional[float] = Field(default=None, ge=0, le=2.0)
```

`backend/app/config.py` 删除（261-262 行）：
```python
    # Merge / export settings
    crossfade_duration: float = 0.1  # seconds; 0 = hard cut (no crossfade)
```

- [ ] **Step 8: 跑全量后端测试**

Run: `uv run --project backend pytest backend/tests/unit/test_ffmpeg_agents.py -q`

Expected: PASS，**16 passed**（22 减去被删的 `TestMergeShotsWithReencoding` 5 个与 `TestMergeShotsWithCrossfade` 1 个）。

Run: `uv run --project backend pytest backend/tests/ -q`

Expected: PASS，无 `ImportError`、无 `AttributeError: 'Settings' object has no attribute 'crossfade_duration'`。

- [ ] **Step 9: 确认没有残留引用**

Run:
```bash
grep -rn "crossfade\|ExportRequest\|merge_shots_with_" backend/ --include=*.py | grep -v "\.venv"
```

Expected: 无输出。

- [ ] **Step 10: Commit**

```bash
git add backend/app/agents/merger.py backend/worker/tasks.py backend/app/api/pipeline.py \
        backend/app/models/schemas.py backend/app/config.py \
        backend/tests/unit/test_ffmpeg_agents.py backend/tests/unit/test_merger_effective.py
git commit -m "refactor(merger): 移除 crossfade，导出改硬切

merge_shots_with_reencoding 走的也是 concat demuxer，对格式不一致的
音频是静默损坏（能播但后半段静音）。三个拼接函数合一，预览与导出
自此走同一条代码路径。"
```

---

### Task 4: 端到端集成测试——真实 /join-preview 端点

**Files:**
- Create: `backend/tests/integration/test_join_preview_vc_audio.py`

**Interfaces:**
- Consumes: `merge_shots`（Task 2）、`build_effective_clip` 的规范化（Task 1）
- Consumes: 既有 fixture `client`、`db_session_factory`，以及 `tests.integration.conftest` 的 `HEADERS`、`_make_project`、`_add_shot`、`seed_shot_with_source`

这一层驱动真实端点、真实 DB、真实 ffmpeg，不 mock 任何东西（没有 LLM 调用，无计费）。这正是用户遇到的场景：shot 1 裁剪、shot 2 音色校准。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/integration/test_join_preview_vc_audio.py`：

```python
"""Regression: the continuity preview must stay in sync when one shot has been
voice-cloned.

The user-visible bug: shot 1 played, then the picture froze on its last frame
the moment shot 2 began.  Cause: shot 2's effective clip was baked at the VC
wav's 24 kHz mono, shot 1's at 48 kHz stereo, and the concat demuxer + -c copy
wrote a single audio decoder config for both -- so shot 2's audio decoded as
garbage and the <video> element's audio-driven clock stalled.

Drives the real /join-preview endpoint against the real DB and real ffmpeg.
"""
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.project import Shot
from app.services.storage import join_preview_path, shot_dir
from tests.integration.conftest import (
    HEADERS, _make_project, _add_shot, seed_shot_with_source,
)


def _make_vc_wav(path: Path, seconds: float = 5.0) -> None:
    """A CosyVoice-shaped wav: 24 kHz, mono, longer than the source video."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=330:sample_rate=24000:duration={seconds}",
         "-ac", "1", "-c:a", "pcm_s16le", str(path)],
        check=True, capture_output=True,
    )


def _stream_duration(path: str, kind: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", f"{kind}:0",
         "-show_entries", "stream=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def _decode_errors(path: str) -> int:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"],
        capture_output=True, text=True,
    )
    return out.stderr.count("Error submitting packet")


@pytest.mark.asyncio
async def test_join_preview_stays_in_sync_with_voice_cloned_shot(
    client, db_session_factory
):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="completed")
    await _add_shot(db_session_factory, pid, 2, status="completed")
    # 120 frames @ 30fps = 4.0s each
    await seed_shot_with_source(db_session_factory, pid, 1, frames=120)
    await seed_shot_with_source(db_session_factory, pid, 2, frames=120)

    # shot 1: trimmed to 60 frames (2.0s).  shot 2: voice-cloned, untrimmed.
    vc_wav = shot_dir(pid, 2) / "audio_vc.wav"
    _make_vc_wav(vc_wav)
    async with db_session_factory() as s:
        shot1 = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot1.trim_frames = 60
        shot2 = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 2)
        )).scalar_one()
        shot2.vc_audio_path = str(vc_wav)
        await s.commit()

    r = await client.post(
        f"/api/projects/{pid}/join-preview",
        json={"shot_ids": [1, 2]},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    previews = sorted(join_preview_path(pid).parent.glob("join_preview*.mp4"))
    assert previews, "no join preview produced"
    out = str(previews[-1])

    # The preview must decode cleanly end to end...
    assert _decode_errors(out) == 0, "preview has audio decode errors"

    # ...and its audio must span the whole 2.0s + 4.0s timeline, not stop at
    # the segment boundary.
    v_dur = _stream_duration(out, "v")
    a_dur = _stream_duration(out, "a")
    assert v_dur == pytest.approx(6.0, abs=0.2), f"video {v_dur}"
    assert a_dur == pytest.approx(v_dur, abs=0.2), (
        f"audio {a_dur} does not span the video {v_dur}"
    )
```

- [ ] **Step 2: 跑测试确认通过**

Run: `uv run --project backend pytest backend/tests/integration/test_join_preview_vc_audio.py -q`

Expected: PASS，1 passed。

若 FAIL，说明 Task 1/2 的修复没覆盖真实端点路径，回到 systematic-debugging 的 Phase 1 重新排查，**不要**放宽断言容差。

这个断言确实抓得住原 bug——已在 Task 2 Step 2 用同形状素材于单测层证明（43 个解码错误、音频 2.03s vs 视频 4.0s）。这里不再重跑一遍「先看它失败」：要在集成层构造修复前状态，得同时回退 `effective_clip.py` 与 `merger.py` 两处改动，而本 worktree 与主检出**共享 git stash 栈**（见 CLAUDE.md），裸 `git stash` / `git stash pop` 有弄丢别的 session 改动的风险。取舍见文末 Self-Review。

- [ ] **Step 3: 跑既有 join-preview 集成测试确认无回归**

Run: `uv run --project backend pytest backend/tests/integration/test_join_preview.py backend/tests/integration/test_join_preview_trim.py -q`

Expected: PASS，5 passed。

注意 `test_join_preview_trim.py` 断言 `88 <= total <= 92`。concat 滤镜重编码后帧数应仍为 90。若落在区间外，**不要**放宽区间——先查 `-vframes` 与滤镜的帧计数是否一致。

- [ ] **Step 4: 跑全量后端测试**

Run: `uv run --project backend pytest backend/tests/ -q`

Expected: PASS，全绿。

- [ ] **Step 5: Commit**

```bash
git add backend/tests/integration/test_join_preview_vc_audio.py
git commit -m "test(join-preview): VC 分镜的连贯性预览音视频同步回归测试

驱动真实端点 + 真实 DB + 真实 ffmpeg，断言零解码错误且音频覆盖
整条时间线。"
```

---

### Task 5: 在真实素材上验证修复

**Files:** 无代码改动（纯验证）

用户报的那个项目里有现成的素材：shot 1 有 `trim_frames=114`，shot 2 有 `vc_audio_path`（24 kHz 单声道）。不触发任何生成，不调 LLM，无计费。

- [ ] **Step 1: 把共享 stack 切到本 worktree**

按 `CLAUDE.md` 的 Local Deploy 流程，从一个已有配置的 worktree 完整拷贝 gitignored 配置：

```bash
SRC=../../..   # 主检出；若其 deploy/secrets 不全，换一个有配置的 worktree
cp -a "$SRC/deploy/secrets"     deploy/secrets
cp -a "$SRC/deploy/secrets.yml" deploy/secrets.yml
cp -a "$SRC/deploy/config.env"  deploy/config.env
( cd frontend-vite && npm ci )
podman compose -f deploy/docker-compose.dev.yml up -d
```

确认挂载切过来了：
```bash
podman inspect video-maker-backend-dev --format '{{range .Mounts}}{{if eq .Destination "/app"}}{{.Source}}{{end}}{{end}}'
```
Expected: 输出包含 `join-preview-audio-mismatch`。

- [ ] **Step 2: 确认 /export 不再接受 crossfade_duration**

```bash
curl -s localhost:8002/openapi.json | python3 -c "import json,sys; s=json.load(sys.stdin); print('ExportRequest' in json.dumps(s))"
```

> 注意：这里的 `python3` 只是解析 curl 输出的一次性 shell 工具，不是项目代码；项目 Python 一律走 `uv`。也可用 `grep -c ExportRequest` 替代。

Expected: `False`。

- [ ] **Step 3: 打开前端，对该项目重跑连贯性预览**

在 `http://localhost:4000` 打开项目 `973a3536-96fb-4388-acfb-0717da51a4f7`，勾选 shot 1 与 shot 2，点击"连贯性预览"。

Expected: 两个分镜连续播放到底，画面不再冻在第一个分镜结尾；第二个分镜有声音。

- [ ] **Step 4: ffprobe 新产出的 preview**

```bash
STORAGE=$(podman volume inspect deploy_app-storage --format '{{.Mountpoint}}')
PREV=$(ls -t "$STORAGE"/projects/973a3536-96fb-4388-acfb-0717da51a4f7/previews/join_preview*.mp4 | head -1)
echo "$PREV"
ffprobe -v error -show_entries stream=codec_type,duration -of csv=p=0 "$PREV"
echo "decode errors: $(ffmpeg -v error -i "$PREV" -f null - 2>&1 | grep -c 'Error submitting packet')"
```

Expected:
- `decode errors: 0`（修复前是 109）
- `video` 与 `audio` 的 duration 相差在 0.2s 以内（修复前是 9.833 vs 4.935）

- [ ] **Step 5: 记录验证结果**

把 Step 4 的实际输出贴进 PR 描述。若 `decode errors` 非 0 或时长仍不对齐，**停下来**，回到 systematic-debugging 的 Phase 1，不要调断言。

---

## Self-Review

**Spec 覆盖检查**

| Spec 要求 | 对应任务 |
|-----------|----------|
| `effective_clip.py` 加 `ar=48000, ac=2` | Task 1 Step 4 |
| `merger.py` 三函数合一、concat 滤镜 | Task 2 Step 3 + Task 3 Step 4 |
| 保留单片段 `c=copy` | Task 2 Step 3；Task 1 Step 6 / Task 3 Step 3 验证 md5 不变性 |
| 保留 `-pix_fmt yuv420p` | Task 2 Step 3；`test_merged_output_is_yuv420p` 覆盖 |
| `run_merger` 去掉 crossfade 参数 | Task 3 Step 5 |
| 删除 `ExportRequest`、导出端点去 body | Task 3 Step 6-7；Task 5 Step 2 验证 |
| 删除 `settings.crossfade_duration` | Task 3 Step 7 |
| 回归测试：零解码错误 + 音视频时长对齐 | Task 2 Step 1（单测）、Task 4 Step 1（集成） |
| 单元测试：VC 片段是 48000/2 | Task 1 Step 1 |
| 保留 join_preview 既有集成测试 | Task 4 Step 3 |
| 清理引用旧函数的测试 | Task 3 Step 2 |
| 「每个分镜必须有音频轨」约束 | Task 2 `test_raises_when_input_has_no_audio` + `_has_audio` 守卫 |

**类型/命名一致性**：`CANONICAL_SAMPLE_RATE` / `CANONICAL_CHANNELS` 在 Task 1 定义于 `merger.py`，Task 1 由 `effective_clip.py` 导入、Task 2 由 `merge_shots` 使用、Task 1 的测试导入断言——四处同名。`merge_shots(shot_paths, output_path, *, vcodec, preset, crf)` 在 Task 2 定义，Task 3 的 `run_merger` 与 `test_merger_effective.py` 均以位置参数调用前两个，签名相容。`_has_audio(path) -> bool` 仅 `merge_shots` 内部使用。

**依赖方向**：`effective_clip.py` → `merger.py`。`merger.py` 只 import `logging`/`subprocess`/`pathlib`/`ffmpeg`，无循环。

**已知偏差**：Task 4 Step 2 没有走「先看它失败」的标准 TDD 节奏——在集成层构造修复前状态需要临时回退两个文件的改动，代价高且易出错。该断言的失败能力已在 Task 2 Step 2 的单测层面用相同素材形状证明（43 个解码错误、音频 2.03s vs 视频 4.0s）。这是有意识的取舍，不是遗漏。
