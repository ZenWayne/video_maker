# 裁剪弹窗 · 声纹波形轨设计

**日期**: 2026-06-28
**状态**: 设计已确认,待实现
**设计稿**: Pencil `design/shots.pen` 帧「裁剪弹窗 · 新增声纹波形轨」(节点 `N3hb4`),导出 `Pictures/screenshot/N3hb4.png`

## 1. 背景与动机

`TrimDialog.tsx`(`frontend-vite/src/components/TrimDialog.tsx`)当前只用一条**纯帧滑块**裁剪视频尾部,用户看不到声音长什么样、尾部静音从哪开始,只能盲调滑块,无法判断自己是切进了人声还是只切了尾部静音。

后端 `video_trimmer.py` **已经有静音检测能力**:

- `detect_speech_end(video_path, silence_threshold_db=-30, min_silence_duration=0.3)` —— 用 ffmpeg `silencedetect` 找**尾部静音起点(= 说话结束点)**,返回秒数或 `None`。
- 「智能校准」按钮(`align-tail-frame` 端点 → `find_best_tail_frame`)已基于这套检测自动裁剪。

痛点:静音裁剪是后端自动算的,UI 上看不见,用户无法**对比**自己的手动裁剪点和自动检测点。

## 2. 目标

在裁剪弹窗中,视频预览区与帧滑块之间,新增一条**与时间轴对齐的声纹波形轨**,让用户:

1. 直观看到人声振幅分布与尾部静音段；
2. 看到后端检测的「说话结束」位置(与「智能校准」同一套检测)；
3. 直接在波形上点击/拖拽设裁剪点,并与现有滑块、帧/时间标签联动。

### 非目标(YAGNI)

- 不做多段裁剪 / 头部裁剪(现有只裁尾部,保持不变)。
- 不做波形缩放/平移、不做逐采样精度的频谱图。
- 不在前端复刻静音检测算法(必须复用后端,见 §4.2)。
- 不改动 `trim` / `restore-trim` / `align-tail-frame` 端点的现有行为。
  - **例外(2026-06-29 实施中确认)**:实现波形复用 `detect_speech_end` 时发现该函数存在既有 bug——AAC 编码视频的尾部静音检测(旧条件 `len(starts) > len(ends)`)恒为 False,导致生产视频上 `detect_speech_end` 恒返回 `None`。因波形「说话结束」线必须复用同一检测,**已在 Task 1 修复此 bug**(新增 `ends[-1] >= duration - 0.15s` 判定)。副作用:`align-tail-frame`(智能校准)与 `suggest_silence_trim` 在 AAC 视频上从"空操作"恢复为正常生效——这是它们本应有的行为。该例外已获用户确认接受,并补充中间静音误判分支的回归测试。

## 3. 三个核心元素及其数据来源

| 元素 | 来源 | 说明 |
|------|------|------|
| **振幅柱(蓝=人声 / 灰=静音)** | 前端 Web Audio `decodeAudioData` | 从已加载的 `output.mp4` 解码 PCM,降采样成峰值数组,画到 `<canvas>`。零后端调用、即时。 |
| **静音区高亮 + 「说话结束」黄线** | 后端 `detect_speech_end`(-30dB / 0.3s) | 必须复用后端同一套检测,用户才能真正"对比"自动裁剪点。通过扩展 `video-info` 端点返回。 |
| **红色裁剪手柄(可拖/点击)** | 前端交互 | 与下方滑块、`endFrame` state 双向联动。 |

## 4. 架构

### 4.1 前端:波形渲染(Web Audio,纯客户端)

新增组件 `WaveformTrack.tsx`,职责单一:**输入视频 URL + 几何/裁剪参数,渲染波形 canvas 并暴露点击/拖拽回调**。

```
WaveformTrack
  props:
    videoSrc: string            // shot.video_path
    fps: number
    totalFrames: number
    endFrame: number            // 当前裁剪点(受控)
    speechEndFrame: number | null  // 后端检测的说话结束帧(只读)
    onScrub: (frame: number) => void  // 点击/拖拽红线 → 上报新裁剪点
  内部:
    - useEffect: fetch(videoSrc) → arrayBuffer → AudioContext.decodeAudioData
    - 把 channelData 降采样为 N 个峰值(N = canvas 宽度 / 每柱像素)
    - 画柱:振幅 > 阈值画蓝(blue-500),否则画灰(zinc-300)
    - 叠加:静音区高亮(speechEndFrame→末尾,amber 18%)、说话结束竖线(amber-700)、
      裁剪手柄竖线+握柄(red-500)、待裁区(speechEnd 右侧 red 12%)
    - 鼠标 down/move/up → 像素 x 换算成 frame → onScrub
  状态:
    - loading(解码中显示骨架)
    - decoded(正常)
    - no-audio / error(视频无音轨或解码失败 → 隐藏波形,回退到纯滑块,不阻塞裁剪)
```

**降采样**:`channelData` 长度 = `sampleRate * duration`(约 26 万样本/6s)。按 canvas 柱数(约 60~120)分桶,每桶取 `max(abs(sample))` 作为峰值。一次性计算,缓存到 ref。

**幅度→颜色阈值**:每桶峰值低于全局峰值的某比例(如 5%)视为静音柱画灰,否则画蓝。注意这只是**视觉提示**;权威静音边界来自后端 `speechEndFrame`(黄线)。

**性能/内存**:`decodeAudioData` 对 6s 音频开销很小(<50ms);解码后立即 `close()` AudioContext 释放。波形只在弹窗 `open` 且视频 URL 变化时重算。

### 4.2 后端:暴露说话结束帧

扩展现有端点 `GET /projects/{id}/shots/{shot_id}/video-info`(`pipeline.py:1102`),在返回体追加两个字段:

```python
info = get_video_info(shot.video_path)               # fps, total_frames, duration
info["has_backup"] = backup.exists()
speech_end_sec = detect_speech_end(shot.video_path)  # 复用现有函数,秒或 None
info["speech_end_sec"] = speech_end_sec
info["speech_end_frame"] = (
    int(speech_end_sec * info["fps"]) if speech_end_sec is not None else None
)
return info
```

- `detect_speech_end` 返回 `None`(无尾部静音 / 整段静音)时,前端不画黄线、不画静音高亮,只画波形。
- `detect_speech_end` 内部已调用一次 `get_video_info` + 一次 ffmpeg `silencedetect`;在此端点合并调用,避免重复 ffprobe(可小幅重构:把已取得的 `info` 传入,或接受一次额外 ffmpeg 调用——端点非热路径,可接受)。

**为何放在 `video-info` 而非新端点**:前端打开弹窗本就调一次 `video-info` 拿 fps/frames;静音帧是同一时刻、同一资源的元数据,合并返回最省往返,符合「最小复用」原则。

### 4.3 前端:`TrimDialog` 集成

- `api.getVideoInfo` 返回类型补 `speech_end_frame: number | null`、`speech_end_sec: number | null`(`api.ts:300`、`types.ts` 的 `VideoInfo`)。
- `TrimDialog` 新增 state `speechEndFrame`,在 `getVideoInfo().then` 里赋值。
- 在视频区与滑块之间插入 `<WaveformTrack ... onScrub={handleSliderChange} />`。
- `onScrub` 复用现有 `handleSliderChange`(已含 `minFrames`/`totalFrames` 钳制 + `seekToFrame`),实现波形↔滑块↔帧标签联动,无需新状态。

### 4.4 关键对齐约束

波形 canvas 的**有效绘制区**(frame 0 → totalFrames 的像素映射)必须与下方滑块轨道**共享完全相同的左右边界**,红色裁剪手柄与滑块圆点才能上下精确对齐。实现上:两者都用 `width: fill_container` 占满 body 内容宽度,且波形帧→像素映射 `x = (frame / totalFrames) * trackWidth`(无额外内边距),与滑块 `(endFrame/totalFrames)*100%` 一致。

## 5. 数据流

```
打开弹窗
  └─ GET video-info ──→ { fps, total_frames, duration, has_backup,
                          speech_end_frame, speech_end_sec }
        ├─ setEndFrame(total_frames) / setFps / ...(现有)
        └─ setSpeechEndFrame(speech_end_frame)        ← 新增

  └─ WaveformTrack mount
        └─ fetch(video_path) → decodeAudioData → 峰值数组 → 画 canvas
             (黄线位置 = speechEndFrame;红线位置 = endFrame)

用户拖红线 / 点波形
  └─ onScrub(frame) → handleSliderChange(frame)
        └─ setEndFrame → 重画红线 + 滑块圆点移动 + 帧/时间标签更新 + video.currentTime seek

确认裁剪 / 智能校准 / 还原:行为不变(现有端点)
```

## 6. 状态与边界(System Status Visibility)

| 状态 | 表现 |
|------|------|
| 波形解码中 | 波形轨显示骨架/脉冲占位,滑块仍可用 |
| 视频无音轨 / 解码失败 | 隐藏波形轨,退化为现有纯滑块体验(不报错、不阻塞裁剪) |
| `speech_end_frame == null` | 画波形,但不画黄线/静音高亮(提示"未检测到尾部静音") |
| 裁剪点已等于说话结束帧 | 黄线与红线重合,说明文字提示"已对齐到说话结束" |
| 当前裁剪点 > 说话结束帧 | 说明文字提示"尾部还有 N 帧静音未裁,拖红线对齐黄线" |

## 7. 测试

### 后端(`uv run pytest`,不涉及 LLM,直接用真实 ffmpeg fixture)
- `video-info` 端点对**有尾部静音**的 fixture 返回非空 `speech_end_frame`,且 `≈ speech_end_sec * fps`。
- 对**无尾部静音 / 整段静音**的 fixture 返回 `speech_end_frame == null`。
- 现有 `video-info` 字段(fps/total_frames/duration/has_backup)不回归。

### 前端(Vitest + 现有 `TrimDialog.test.tsx`)
- `getVideoInfo` mock 含 `speech_end_frame` 时,`TrimDialog` 渲染 `WaveformTrack` 并传入正确 props。
- `onScrub(frame)` → 帧标签、滑块值更新(复用 `handleSliderChange` 路径)。
- `speech_end_frame == null` 时不渲染黄线相关元素。
- `WaveformTrack` 在 `decodeAudioData` reject 时退化为隐藏波形、不抛错(mock AudioContext)。
- 注意:Playwright 中凡触发 AI 的端点继续 mock;`video-info`/`trim` 非 AI,但 `trim` 会改素材,e2e 中按需 mock。

## 8. 素材文件审计(遵循 CLAUDE.md「Shot 素材文件变更审计」)

本变更**只读** `shot.video_path` 解码音频与检测静音,**不写入 / 重命名 / 删除任何素材文件**,不改变 `trim`/`restore`/`vc`/`cc` 逻辑。因此:

- 波形与静音检测始终基于 `shot.video_path`(DB 字段),不硬编码文件名 ✓
- 不产生新备份、不需重置 `vc_status`/`cc_status` ✓
- 裁剪/还原/VC/CC 后 `video_path` 变化 → 弹窗重新打开会重新 `video-info` + 重新解码波形,自动反映最新素材,不会读到过期文件 ✓

## 9. 影响文件清单

| 文件 | 改动 |
|------|------|
| `backend/app/api/pipeline.py` | `video-info` 端点追加 `speech_end_frame` / `speech_end_sec` |
| `backend/tests/...` | 新增 `video-info` 静音帧用例 |
| `frontend-vite/src/lib/api.ts` | `getVideoInfo` 返回类型补字段 |
| `frontend-vite/src/lib/types.ts` | `VideoInfo` 补字段 |
| `frontend-vite/src/components/WaveformTrack.tsx` | **新增**波形组件 |
| `frontend-vite/src/components/TrimDialog.tsx` | 集成波形轨 + `speechEndFrame` state |
| `frontend-vite/src/components/__tests__/TrimDialog.test.tsx` | 补测 |

## 10. 设计风格

完全沿用 `design/shots.pen` 既有设计系统:`zinc-*` 中性色、`blue-500`(人声/进度)、`red-500`(裁剪点)、`amber-700`(说话结束)、Inter 字体、圆角 8。
