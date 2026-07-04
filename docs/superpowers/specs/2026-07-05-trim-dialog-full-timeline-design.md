# 裁剪弹窗：全段时间轴 + 已裁剪灰显 + 静音参考帧数 — 设计文档

- **日期**: 2026-07-05
- **状态**: 设计已确认（Pencil mock: `design/shots.pen` 节点 `N3hb4`）
- **范围**: 前端 `TrimDialog.tsx` / `WaveformTrack.tsx`；后端不改（`/waveform` 已提供全源峰值）

## 背景 / 问题

1. **帧条只显示剪完的范围**：一个已裁剪（非破坏 EDL，`trim_frames` 已设）的 shot
   再次打开裁剪弹窗时，滑块与帧数显示的是剪后长度——被裁掉的尾段在时间轴上消失，
   无法把裁剪点往回拖恢复。
2. **静音检测结果缺少数值展示**：波形轨上有说话结束黄线，但没有对应帧数的文字，
   用户无法精确知道参考点在第几帧。

## 设计（以 Pencil mock `N3hb4` 为准）

**核心原则：裁剪弹窗永远显示完整源时间轴；"已裁剪"是一种视觉状态（灰），不是数据缺失。**

| 区域 | 行为 |
|------|------|
| 波形轨 | 全源峰值。蓝色=人声；黄色带+黄线=尾部静音/说话结束帧；红线（可拖）=裁剪点；**红线右侧覆盖磨砂灰（波形透灰可见）= 已裁剪** |
| 帧条（滑块） | **全段**：`min..source_frames`。蓝色段到裁剪点，其后灰色段（`$zinc-300`），与波形灰区对齐 |
| 帧信息行 | `帧: {endFrame} / {source_frames}` · `裁掉 N 帧`（红）· **`静音参考: 第 {speechEndFrame} 帧`**（琥珀色，仅在检测有结果时显示） |
| 图例 | `蓝=人声 · 灰=已裁剪 · 黄线=说话结束 · 红线=裁剪点 · 绿线=播放` |
| 底部动作 | 不变：还原 / 智能校准 / 静音裁剪 ｜ 取消 / 确认裁剪 |

## 实现要点

1. **totalFrames 改用源全长**：弹窗打开时 `totalFrames = shot.source_frames`（EDL
   源总帧数），而非剪后长度；`endFrame` 初始化为当前 `trim_frames`（无裁剪则为
   source_frames）。播放/预览仍受 EDL 影响的地方需确认 seek 用的是源帧号。
   **回退**：`source_frames` 为空（未经 EDL 生成的老 shot）时沿用现行为——
   取 video-info 的实际总帧数（此时文件即全源，语义一致）。
2. **WaveformTrack**：新增"已裁剪灰显"——`endFrame` 右侧区域画灰色覆盖
   （替换现红色薄膜 `bg-red-*`），bars 本身不重取。
3. **帧条**：滑块 `max = source_frames`；进度条剪除段颜色红改灰。
4. **静音参考帧数**：`detectSilence` 返回的 `suggested_end_frame` 存入 state，
   在帧信息行以琥珀色常显（与黄线同色系）；再次打开弹窗时若已有
   `speech_end_frame`（video-info 已暴露）直接显示，无需重新检测。
5. **后端不动**：`GET /waveform` 已对完整 `shot.video_path` 提峰值；
   `video-info` 已暴露 `speech_end_frame/sec`。

## 测试

- TrimDialog 单测：已裁剪 shot（`trim_frames < source_frames`）打开弹窗 →
  滑块 max = source_frames、endFrame = trim_frames、裁剪点可往右拖回。
- WaveformTrack 单测：endFrame 右侧渲染灰色覆盖层（快照/样式断言）。
- 静音参考显示：`speech_end_frame` 存在时帧信息行渲染 `静音参考: 第 N 帧`。
- e2e（遵循项目规则，不 mock 被测数据流）：真实 DB 种子一个已裁剪 shot，
  打开弹窗断言全段时间轴与灰显；`detect-silence` 为只读端点、不计费，可真调。

## 非目标

- 不做「采用参考」一键吸附按钮（另议）。
- 不改静音检测算法与 `/waveform`、`/detect-silence` 端点。
- 不改 ShotPlayer 的 EDL 播放钳制。
