# 裁剪弹窗全段时间轴 + 已裁剪灰显 + 静音参考帧数 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 裁剪弹窗永远显示完整源片时间轴：已裁剪部分在波形/帧条上灰显、裁剪点可回拖，静音参考帧数常显。

**Architecture:** 后端三个只读端点（video-info / waveform / detect-silence）统一改用 `shot_source_path()`（源片视角，与 trim 端点同帧坐标系），video-info 额外返回 `source_video_url`；前端 TrimDialog 用源片 URL 做预览、帧条剪除段红改灰、帧信息行加静音参考；WaveformTrack 裁剪区红膜改磨砂灰、图例更新。

**Tech Stack:** FastAPI + pytest（后端，mock 掉 ffmpeg 函数）；React + vitest + @testing-library（前端）；Playwright（e2e）。

## Global Constraints

- 测试绝不跑真 ffmpeg / 真模型（monkeypatch `app.agents.video_trimmer` 各函数）。
- 后端测试用 `uv run --project backend pytest`（不经 podman）。
- e2e 只允许 mock AI 触发端点为真实 202 形状；本任务后端行为由 Task 1 集成测试覆盖，e2e 只断言前端渲染。
- 不写任何硬编码绝对路径。
- 改完后端代码 `podman restart video-maker-backend-dev video-maker-worker-dev`。
- 设计基准：Pencil mock `design/shots.pen` 节点 `N3hb4`；spec：`docs/superpowers/specs/2026-07-05-trim-dialog-full-timeline-design.md`。

---

### Task 1: 后端 — 裁剪弹窗只读端点统一源片视角

**Files:**
- Modify: `backend/app/api/pipeline.py`（`get_shot_video_info` ~L1494、`get_shot_waveform` ~L1526、`detect_silence` ~L1815）
- Test: `backend/tests/integration/test_trim_dialog_source_view.py`（新建）

**Interfaces:**
- Consumes: `app.services.storage.shot_source_path(project_id, shot_id) -> Optional[Path]`（已存在，trim 端点同款）、`to_media_url`
- Produces: `GET video-info` 响应新增字段 `source_video_url: str | None`；三端点的帧号/峰值均基于源片（Task 3 依赖 `source_video_url`）

- [ ] **Step 1: 写失败测试**

```python
"""裁剪弹窗只读端点必须以源片（output_*.mp4）为准，而非 video_path 指向的派生文件。

场景：老 shot / VC 后 shot 的 video_path 指向物理剪过的 vc_*.mp4，
若端点直接 ffprobe/提峰值/静音检测该文件，时间轴只剩剪后长度。
"""
from pathlib import Path

import pytest

import app.agents.video_trimmer as vt
from tests.integration.conftest import HEADERS, _make_project
from app.models.project import Shot


async def _seed_shot_with_derived_video(sf, tmp_path, pid):
    """shot 目录里放 output_*.mp4（源片）+ vc_*.mp4（派生），video_path 指派生文件。"""
    shot_dir = tmp_path / "projects" / pid / "shots" / "shot_1"
    shot_dir.mkdir(parents=True)
    source = shot_dir / "output_1700000000_deadbeef.mp4"
    source.write_bytes(b"src")
    derived = shot_dir / "vc_1700000001_cafebabe.mp4"
    derived.write_bytes(b"vc")
    async with sf() as s:
        s.add(Shot(
            project_id=pid, shot_id=1, text="t", shot_type="Medium Shot",
            visual_description="v", shot_duration=6, status="completed",
            align_with_previous=False, video_path=str(derived),
        ))
        await s.commit()
    return str(source), str(derived)


@pytest.fixture
def probe_calls(monkeypatch):
    """Mock 掉所有 ffmpeg 函数，记录每个函数收到的视频路径。"""
    calls = {}

    def _rec(key, ret):
        def f(path, *args, **kwargs):
            calls[key] = path
            return ret
        return f

    monkeypatch.setattr(vt, "get_video_info", _rec("info", {
        "fps": 24.0, "total_frames": 144, "duration": 6.0,
    }))
    monkeypatch.setattr(vt, "speech_end_info", _rec("speech", (5.0, 120)))
    monkeypatch.setattr(vt, "extract_waveform_peaks", _rec("peaks", [0.5]))
    monkeypatch.setattr(vt, "suggest_silence_trim", _rec("silence", None))
    return calls


async def test_video_info_probes_source_and_returns_source_url(
    client, db_session_factory, tmp_path, probe_calls
):
    pid = await _make_project(db_session_factory, status="shot_review")
    source, _derived = await _seed_shot_with_derived_video(db_session_factory, tmp_path, pid)

    r = await client.get(f"/api/projects/{pid}/shots/1/video-info")
    assert r.status_code == 200
    assert probe_calls["info"] == source, "ffprobe 必须打在源片上"
    assert probe_calls["speech"] == source, "静音检测必须打在源片上"
    assert r.json()["source_video_url"] is not None
    assert "output_1700000000_deadbeef.mp4" in r.json()["source_video_url"]


async def test_waveform_extracts_from_source(
    client, db_session_factory, tmp_path, probe_calls
):
    pid = await _make_project(db_session_factory, status="shot_review")
    source, _ = await _seed_shot_with_derived_video(db_session_factory, tmp_path, pid)

    r = await client.get(f"/api/projects/{pid}/shots/1/waveform")
    assert r.status_code == 200
    assert probe_calls["peaks"] == source


async def test_detect_silence_probes_source(
    client, db_session_factory, tmp_path, probe_calls
):
    pid = await _make_project(db_session_factory, status="shot_review")
    source, _ = await _seed_shot_with_derived_video(db_session_factory, tmp_path, pid)

    r = await client.post(f"/api/projects/{pid}/shots/1/detect-silence", headers=HEADERS)
    assert r.status_code == 200
    assert probe_calls["silence"] == source


async def test_video_info_falls_back_to_video_path_without_source(
    client, db_session_factory, tmp_path, probe_calls
):
    """无 output_*.mp4（异常/极老数据）时回退 video_path，不 500。"""
    pid = await _make_project(db_session_factory, status="shot_review")
    shot_dir = tmp_path / "projects" / pid / "shots" / "shot_1"
    shot_dir.mkdir(parents=True)
    only = shot_dir / "vc_1700000001_cafebabe.mp4"
    only.write_bytes(b"vc")
    async with db_session_factory() as s:
        s.add(Shot(
            project_id=pid, shot_id=1, text="t", shot_type="Medium Shot",
            visual_description="v", shot_duration=6, status="completed",
            align_with_previous=False, video_path=str(only),
        ))
        await s.commit()

    r = await client.get(f"/api/projects/{pid}/shots/1/video-info")
    assert r.status_code == 200
    assert probe_calls["info"] == str(only)
```

注意 conftest 的 `client` fixture 已把 `settings.storage_root` 指到 `tmp_path`，
所以 `shot_source_path`（内部按 `storage_root/projects/...` 找 `output_*.mp4`）
能命中我们种的文件。`speech_end_info`/`suggest_silence_trim` 的 mock 用
`setdefault(...) and X or Y` 保证既记录路径又返回正确形状。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run --project . pytest tests/integration/test_trim_dialog_source_view.py -q`
Expected: FAIL — `probe_calls["info"]` 等于 derived 路径而非 source；`source_video_url` KeyError。

- [ ] **Step 3: 实现**

`backend/app/api/pipeline.py`，在 `get_shot_video_info` 上方加 helper：

```python
def _dialog_source(project_id: str, shot_id: int, video_path: str) -> str:
    """裁剪弹窗只读端点的统一「源片视角」。

    trim 端点按源片帧号裁剪（shot_source_path），弹窗展示的时间轴/波形/静音
    检测必须基于同一文件，否则 VC 后（video_path 指向物理剪过的派生文件）
    时间轴只剩剪后长度。找不到源片时回退 video_path。
    """
    from app.services.storage import shot_source_path
    src = shot_source_path(project_id, shot_id)
    return str(src) if src is not None else video_path
```

`get_shot_video_info` 内，把：

```python
    info = get_video_info(shot.video_path)
```

改为：

```python
    source = _dialog_source(project_id, shot_id, shot.video_path)
    info = get_video_info(source)
```

同函数内 `speech_end_info(shot.video_path, info["fps"])` 改为
`speech_end_info(source, info["fps"])`；`return info` 前加：

```python
    info["source_video_url"] = to_media_url(source)
```

`get_shot_waveform` 内 `extract_waveform_peaks(shot.video_path)` 改为：

```python
        peaks = extract_waveform_peaks(_dialog_source(project_id, shot_id, shot.video_path))
```

`detect_silence` 内把 `suggest_silence_trim(shot.video_path)` 与其后
`get_video_info(shot.video_path)`（含无静音早返回分支里的那处）全部改为用
同一个 `source = _dialog_source(project_id, shot_id, shot.video_path)` 变量。

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `uv run --project . pytest tests/integration/test_trim_dialog_source_view.py -q` → 4 passed
Run: `uv run --project . pytest tests/ -q` → 与 master 基线一致（已知 3 个既有失败与本任务无关）

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/pipeline.py backend/tests/integration/test_trim_dialog_source_view.py
git commit -m "feat(trim): dialog read endpoints use source video view + source_video_url"
```

---

### Task 2: WaveformTrack — 已裁剪区磨砂灰显 + 图例更新

**Files:**
- Modify: `frontend-vite/src/components/WaveformTrack.tsx:58-63`（红膜→灰）、`:91-93`（图例）
- Test: `frontend-vite/src/components/__tests__/WaveformTrack.test.tsx`（追加）

**Interfaces:**
- Consumes: 现有 props（`peaks/totalFrames/endFrame/...`），无新增。
- Produces: canvas 裁剪区颜色 `rgba(244, 244, 245, 0.78)`（Task 4 e2e 不直接断言色值，Task 2 单测断言）。

- [ ] **Step 1: 写失败测试**（追加到 WaveformTrack.test.tsx，沿用文件里现成的 `fillStyleLog`）

```tsx
  it('endFrame 右侧画磨砂灰覆盖(已裁剪灰显)，不再画红膜', () => {
    render(
      <WaveformTrack
        peaks={samplePeaks}
        totalFrames={240}
        endFrame={200}
        speechEndFrame={180}
        onScrub={() => {}}
      />,
    )
    expect(fillStyleLog).toContain('rgba(244, 244, 245, 0.78)')
    expect(fillStyleLog).not.toContain('rgba(239, 68, 68, 0.12)')
  })

  it('图例说明包含 灰=已裁剪', () => {
    render(
      <WaveformTrack
        peaks={samplePeaks}
        totalFrames={240}
        endFrame={200}
        speechEndFrame={null}
        onScrub={() => {}}
      />,
    )
    expect(
      screen.getByText('蓝=人声 · 灰=已裁剪 · 黄线=说话结束 · 红线=裁剪点 · 绿线=播放'),
    ).toBeInTheDocument()
  })
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend-vite && npx vitest run src/components/__tests__/WaveformTrack.test.tsx`
Expected: 新增 2 条 FAIL（旧色值仍在 / 图例文案不匹配）。

- [ ] **Step 3: 实现**

`WaveformTrack.tsx` 把：

```ts
    // 待裁区 + 裁剪竖线
    const cx = pixelForFrame(endFrame, width, totalFrames)
    g.fillStyle = 'rgba(239, 68, 68, 0.12)' // red 12%
    g.fillRect(cx, 0, width - cx, TRACK_HEIGHT)
```

改为：

```ts
    // 已裁剪区磨砂灰显（波形透灰可见）+ 裁剪竖线
    const cx = pixelForFrame(endFrame, width, totalFrames)
    g.fillStyle = 'rgba(244, 244, 245, 0.78)' // zinc-100 磨砂
    g.fillRect(cx, 0, width - cx, TRACK_HEIGHT)
```

图例 span 文案改为：

```tsx
          蓝=人声 · 灰=已裁剪 · 黄线=说话结束 · 红线=裁剪点 · 绿线=播放
```

- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/components/__tests__/WaveformTrack.test.tsx` → 全 PASS

- [ ] **Step 5: Commit**

```bash
git add frontend-vite/src/components/WaveformTrack.tsx frontend-vite/src/components/__tests__/WaveformTrack.test.tsx
git commit -m "feat(trim): grey out trimmed region on waveform, update legend"
```

---

### Task 3: TrimDialog — 源片预览 + 帧条灰段 + 静音参考帧数

**Files:**
- Modify: `frontend-vite/src/lib/api.ts:336-343`（getVideoInfo 类型加 `source_video_url`）
- Modify: `frontend-vite/src/components/TrimDialog.tsx`（state ~L43、载入 effect ~L132、`<video>` ~L278、滑块 ~L305、帧信息 ~L322）
- Test: `frontend-vite/src/components/__tests__/TrimDialog.test.tsx`（改 mock + 追加断言）

**Interfaces:**
- Consumes: Task 1 的 `video-info.source_video_url`。
- Produces: 无下游依赖（e2e 只读文案/控件）。

- [ ] **Step 1: 写失败测试**（TrimDialog.test.tsx）

先把文件顶部 `getVideoInfo` 的 mock 返回值扩为（保持其余字段不变）：

```ts
    getVideoInfo: vi.fn().mockResolvedValue({
      fps: 24,
      total_frames: 240,
      duration: 10.0,
      has_backup: false,
      speech_end_frame: 180,
      speech_end_sec: 7.5,
      source_video_url: '/api/media/projects/p/shots/shot_1/output_1_a.mp4',
    }),
```

追加测试（`baseShot` 为文件内现有 fixture；渲染辅助沿用现有写法）：

```tsx
  it('帧信息行常显静音参考帧数(琥珀色)', async () => {
    renderDialog()
    expect(await screen.findByText('静音参考: 第 180 帧')).toBeInTheDocument()
  })

  it('预览 video 使用源片 URL 而非 shot.video_path', async () => {
    renderDialog()
    await screen.findByText(/帧:/)
    const video = document.querySelector('video')!
    expect(video.getAttribute('src')).toBe('/api/media/projects/p/shots/shot_1/output_1_a.mp4')
  })

  it('已裁剪 shot 打开时时间轴仍是全段、裁剪点在 trim_frames', async () => {
    renderDialog({ ...baseShot, trim_frames: 200 })
    expect(await screen.findByText(/帧:\s*200\s*\/\s*240/)).toBeInTheDocument()
    expect(screen.getByText('裁掉 40 帧')).toBeInTheDocument()
    const slider = document.querySelector('input[type="range"]') as HTMLInputElement
    expect(slider.max).toBe('240')
  })
```

（若现有文件没有 `renderDialog(shotOverride?)` 辅助，按文件内现有 render 方式
内联展开，shot prop 传 override。）

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run src/components/__tests__/TrimDialog.test.tsx`
Expected: 新增 3 条中「静音参考」「源片 URL」两条 FAIL；全段一条应已 PASS（video-info mock 的
total_frames 即全长——它是回归保护，防止实现改坏初始化）。

- [ ] **Step 3: 实现**

`api.ts` getVideoInfo 返回类型加一行：

```ts
    source_video_url?: string | null
```

`TrimDialog.tsx`：

state 区（~L43 后）加：

```ts
  const [sourceVideoUrl, setSourceVideoUrl] = useState<string | null>(null)
```

载入 effect 里 `setSpeechEndFrame(info.speech_end_frame)` 之后加：

```ts
      setSourceVideoUrl(info.source_video_url ?? null)
```

`<video>` 的 src 改为：

```tsx
                src={sourceVideoUrl ?? shot.video_path ?? undefined}
```

滑块进度条剪除段（~L305）：

```tsx
                <div
                  className="absolute inset-y-0 bg-zinc-300 rounded-r-full"
                  style={{ left: `${trimmedPercent}%`, right: 0 }}
                />
```

帧信息行（`裁掉 N 帧` span 之后、`playheadFrame` span 之前）加：

```tsx
                {speechEndFrame != null && (
                  <span className="text-amber-700 ml-2 font-medium">
                    静音参考: 第 {speechEndFrame} 帧
                  </span>
                )}
```

- [ ] **Step 4: 跑测试确认通过 + 组件全量**

Run: `npx vitest run` → 全 PASS（既有 47 条 + 本次新增）

- [ ] **Step 5: Commit**

```bash
git add frontend-vite/src/lib/api.ts frontend-vite/src/components/TrimDialog.tsx frontend-vite/src/components/__tests__/TrimDialog.test.tsx
git commit -m "feat(trim): full-source timeline in dialog — source video preview, grey cut segment, silence reference frames"
```

---

### Task 4: e2e — 弹窗全段时间轴与新文案

**Files:**
- Modify: `frontend-vite/e2e/waveform-trim.spec.ts`（mockVideoInfo 加 `source_video_url`；mock shot 加 `trim_frames`；追加断言）

**Interfaces:**
- Consumes: Task 2 图例文案、Task 3 静音参考文案与全段帧显示。
- Produces: 无。

说明：该 spec 文件是既有的 hermetic 波形用例（文件头有豁免理由：Chromium 无法解码
shot MP4，峰值由后端产生、后端行为另有真实测试覆盖）。本任务遵循同一模式，只
断言前端渲染；后端源片视角行为由 Task 1 的真实集成测试覆盖，不在 e2e 里伪造被测数据流。

- [ ] **Step 1: 更新 mock 数据 + 追加用例**

`mockVideoInfo` 加：

```ts
  source_video_url: REAL_VIDEO,
```

`mockProject.shots[0]` 加（模拟已裁剪 shot）：

```ts
      trim_frames: 100,
      source_fps: 24.0,
      source_frames: 117,
```

追加测试：

```ts
  test('已裁剪 shot：全段时间轴 + 灰显图例 + 静音参考帧数', async ({ page }) => {
    await page.goto(`/projects/${PROJECT_ID}/shots`)
    await expect(page.getByTestId('shots-list')).toBeVisible({ timeout: 10_000 })
    await page.getByRole('button', { name: '裁剪' }).first().click()
    await expect(page.getByText('裁剪视频 — Shot #1')).toBeVisible({ timeout: 5_000 })

    // 全段：分母是源总帧 117，裁剪点回落在 trim_frames=100
    await expect(page.getByText(/帧:\s*100\s*\/\s*117/)).toBeVisible()
    await expect(page.getByText('裁掉 17 帧')).toBeVisible()
    // 静音参考帧数常显（mockVideoInfo.speech_end_frame = 104）
    await expect(page.getByText('静音参考: 第 104 帧')).toBeVisible()
    // 图例含灰显说明
    await expect(page.getByText(/灰=已裁剪/)).toBeVisible()
  })
```

- [ ] **Step 2: 跑 e2e**

前置：共享 stack 正在运行（端口 4000/8002）。
Run: `npx playwright test e2e/waveform-trim.spec.ts`
Expected: 既有 2 条 + 新 1 条全部 PASS。

- [ ] **Step 3: Commit**

```bash
git add frontend-vite/e2e/waveform-trim.spec.ts
git commit -m "test(e2e): trim dialog full timeline, grey legend, silence reference"
```

---

### Task 5: 部署验证 + 收尾

**Files:** 无新改动（验证 + PR）

- [ ] **Step 1: 切换/刷新 stack 到本 worktree 并重启**

```bash
cd <本 worktree>
cp -a ../../../deploy/secrets deploy/secrets && cp -a ../../../deploy/secrets.yml deploy/secrets.yml && cp -a ../../../deploy/config.env deploy/config.env
( cd frontend-vite && npm ci )
podman compose -f deploy/docker-compose.dev.yml up -d
curl -s localhost:8002/openapi.json | grep -o source_video_url || curl -s "localhost:8002/api/projects/<真实项目id>/shots/1/video-info" | grep source_video_url
```

（重启 worker 前照例确认没有在跑的生成任务。）

- [ ] **Step 2: 真实项目人工验收**

用一个已裁剪过的真实 shot 打开裁剪弹窗：时间轴为全段、裁剪点可往回拖、
灰显区与帧条灰段对齐、静音参考帧数显示。**不触发任何生成。**

- [ ] **Step 3: push + draft PR**

```bash
git push -u origin worktree-trim-dialog-full-timeline-spec
gh pr create --draft --title "feat(trim): 裁剪弹窗全段时间轴 + 已裁剪灰显 + 静音参考帧数" --body "spec: docs/superpowers/specs/2026-07-05-trim-dialog-full-timeline-design.md"
```
