# 连贯性预览音频格式不匹配 — 设计文档

**日期**: 2026-07-10
**状态**: 已确认根因，待实现

## 问题

选中两个分镜（其中 shot 2 做过音色校准）点击"连贯性预览"，第一个分镜正常播放，进入第二个分镜时画面冻结在第一个分镜的最后一帧，不再前进。

## 根因

`join_preview` 端点用 ffmpeg 的 **concat demuxer + `-c copy`** 拼接分镜（`merge_shots()`，`backend/app/agents/merger.py:135`）。stream copy 要求所有输入的编码参数完全一致。

而 `build_effective_clip()`（`backend/app/agents/effective_clip.py:49`）在替换 VC 音频时只指定了 `acodec="aac"`，没有指定采样率和声道数，于是直接继承了 VC wav 的格式：

| 分镜 | 处理 | 输出音频格式 |
|------|------|--------------|
| shot 1 | trim（重编码） | AAC **48000 Hz 立体声** |
| shot 2 | VC（重编码） | AAC **24000 Hz 单声道** |

MP4 容器只写一份音频解码配置，取自第一段（48k 立体声）。shot 2 的每个音频包因此被喂给一个配置错误的解码器。

### 为什么画面会冻住

`<video>` 元素的播放时钟由音频驱动。音频解码在段边界崩掉后媒体元素停摆，那条本身完全正常的视频轨再也没往前走一帧。冻结位置正是 shot 1 裁剪后的末尾。

### 证据

对 UI 实际生成的 `join_preview_1783651677_16eea23d.mp4` 取证：

- 视频轨 236 帧 / 9.833s — 正常（114 + 122 帧）
- 音频轨仅 **4.935s**，解码报 109 个 AAC 错误（`Input buffer exhausted before END element found`、`channel element 2.2 is not allocated`）
- 段边界后音频包时间戳塌缩为每包 1/48000 秒

用同样参数重新烘焙两个 effective clip 并跑 `-c copy` concat，输出为 `video=9.833333, audio=4.935000` — 与线上文件逐位相同。复现确定。

## 附带发现：重编码 concat 是静默损坏

`merge_shots_with_reencoding()` 用的仍是 concat demuxer，解码器同样只配置一次。实测其输出音频只有 4.913s，**但不再报任何解码错误** — 文件能正常播放，后半段却是静音。

该函数今天就在被调用：导出时若 `crossfade_duration <= 0` 即走此路径。因此**关闭转场的导出成片中，VC 过的分镜是没有声音的**。默认走 crossfade（`acrossfade` 滤镜会自动重采样）掩盖了这一点。

## 方案

**一个拼接函数 + 一种规范音频格式。**

### 第一层：`effective_clip.py` 固定输出格式

`build_effective_clip()` 的 `opts` 增加 `ar=48000, ac=2`。VC 片段不再继承 24k mono，从源头消除格式差异。透传的 Veo 原片本就是 48k 立体声，两边收敛到同一规范格式。

### 第二层：`merger.py` 改用 concat 滤镜

三个拼接函数（`merge_shots`、`merge_shots_with_reencoding`、`merge_shots_with_crossfade`）合并为一个 `merge_shots()`，用 concat **滤镜**而非 demuxer：

```
-filter_complex "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v][a]"
-map [v] -map [a] -c:v libx264 -preset fast -crf 18 -pix_fmt yuv420p
-c:a aac -ar 48000 -ac 2
```

滤镜为每个输入各自建解码器并自动重采样，对任何残余的参数不一致免疫。

单片段输入保留 `c=copy` 分支 — `test_merger_effective.py` 依赖它做末帧 md5 逐字节校验。

`-pix_fmt yuv420p` 保留：浏览器和多数硬件解码器无法解码 `High 4:4:4 Predictive`。

### 移除 crossfade

- 删除 `merge_shots_with_crossfade`、`merge_shots_with_reencoding`、`_get_durations`
- `run_merger` 去掉 `crossfade_duration` 参数，改调 `merge_shots`
- 删除 `ExportRequest`（前端从未传过该字段），导出端点去掉 `body` 参数
- 删除 `settings.crossfade_duration`

导出成片变为硬切，无转场。**已与用户确认这是期望行为。**

### 附带好处

连贯性预览与导出走完全同一条代码路径。此前预览是 stream-copy、导出是 xfade 重编码，预览通过并不代表导出是那个样子。合并之后，预览看到的就是导出会得到的。

## 实测数据

| 方案 | 解码错误 | 视频时长 | 音频时长 | 耗时 |
|------|---------|---------|---------|------|
| 现状：concat demuxer + `-c copy` | 109 | 9.833s | 4.935s ✗ | 0.07s |
| concat demuxer + 重编码 | 0 | 9.833s | 4.914s ✗（静默损坏） | — |
| 仅第一层：normalize + `-c copy` | 0 | 9.833s | 9.849s ✓ | 0.07s |
| **两层：normalize + concat 滤镜** | **0** | **9.833s** | **9.834s ✓** | **0.79s** |

预览耗时从 0.07s 增至 0.79s（2 分镜 / 10 秒素材，720x1280，`preset=fast crf=18`）。同步端点可接受，约 5 分镜时 ~2s。

## 风险与约束

- **concat 滤镜要求所有输入的分辨率与 SAR 一致**，不一致会直接报错。同项目内 Veo 输出尺寸统一，约束成立。真不成立时，报错优于今天的静默损坏。
- **每个分镜必须有音频轨**。Veo 输出均有。
- 单分镜导出仍走 `c=copy`，末帧 md5 不变性保持。

## 需改动的文件

| 文件 | 改动 |
|------|------|
| `backend/app/agents/effective_clip.py` | `opts` 加 `ar=48000, ac=2` |
| `backend/app/agents/merger.py` | 三函数合一，改用 concat 滤镜 |
| `backend/worker/tasks.py` | `run_merger` 去掉 crossfade 参数 |
| `backend/app/api/pipeline.py` | 导出端点去掉 body；预览调用不变 |
| `backend/app/models/schemas.py` | 删除 `ExportRequest` |
| `backend/app/config.py` | 删除 `crossfade_duration` |

## 测试

**回归测试（今天的代码会失败）**：烤一个 48k 立体声片段和一个 24k 单声道片段，`merge_shots` 拼接，断言：
1. `ffmpeg -f null -` 零解码错误
2. 音频时长与视频时长对齐（容差一个音频帧）

**单元测试**：`build_effective_clip` 喂 24k mono wav，断言输出 `sample_rate=48000, channels=2`。

**保留**：`yuv420p` 断言；`test_merger_effective.py` 单分镜末帧 md5 不变性；`test_join_preview.py` / `test_join_preview_trim.py` 集成测试。

**清理**：`TestMergeShotsWithCrossfade` 与引用 `merge_shots_with_reencoding` 的测试改指向新的 `merge_shots`。
