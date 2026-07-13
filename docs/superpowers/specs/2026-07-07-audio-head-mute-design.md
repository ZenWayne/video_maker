# 前段静音(音频剪头)— 设计文档

- **日期**: 2026-07-07
- **状态**: 设计已确认(brainstorming),待用户评审后转实现计划
- **范围**: 后端 `models/project.py`、`db.py`、`agents/effective_clip.py`、`agents/video_trimmer.py`、`api/pipeline.py`、序列化;前端 `TrimDialog.tsx`、`WaveformTrack.tsx`、`ShotPlayer.tsx`、`useShotSync.ts`、`api.ts`、`types.ts`

## 需求

把一个分镜视频**开头一小段的声音静音**——视频照常播放,开头一小段没有声音,后面的语音**保持在原来的时间位置不动**(A/V、口型不错位)。典型用途:去掉说话前的呼吸声、喷麦、杂音。

用户确认的两个关键决策:
1. **语义 = 静音前段**(不是前移音频、也不是连画面一起裁头)。
2. **输入方式 = 波形前手柄 + 自动检测助手**。

## 方案(A:接进现有非破坏 EDL)

和现有 `trim_frames`(裁尾)/`vc_audio_path`(换整条音轨)完全同构:新增一个"前 N 帧静音"字段,渲染时在 `build_effective_clip` 一处用一个音频滤镜应用,播放器同步静音。非破坏、可还原、与 trim/vc 正交叠加。

否决的替代:B 破坏式重 mux(违背非破坏设计、不可还原);C 通用任意区间静音列表(用户只要前段,YAGNI)。

## 1. 数据模型(EDL 字段)

`Shot` 新增:

```python
audio_head_mute_frames = Column(Integer, nullable=True)  # 静音音频前 [0,N) 帧；None/0=不静音
```

- 用**帧**:与 `trim_frames` 对称、与波形帧↔像素映射一致、帧精度。
- `db.py` 加幂等迁移(`ALTER TABLE shots ADD COLUMN audio_head_mute_frames INTEGER`)。
- 序列化(`schemas.py` ShotResponse、`projects.py`、`stream.py`)附带算出
  `audio_head_mute_sec = audio_head_mute_frames / source_fps`(source_fps 存在且 frames>0 时,否则 None),供前端播放器用——与 `trim_end_sec` 同款。

## 2. 渲染(`build_effective_clip` 一处应用)

现有函数按 `trim_frames` / `vc_audio_path` 决定音源与时长。追加:当 `audio_head_mute_frames > 0` 时,在音频链末尾加滤镜

```
volume=enable='lt(t,{mute_sec})':volume=0
```

`mute_sec = audio_head_mute_frames / fps`。只把 `t < mute_sec` 的音频压到 0,其余原样、时间轴不动 → A/V 不错位。

叠加顺序(正交):选音源(源音 `0:a` 或 vc `1:a`)→ 按 trim 裁时长(atrim)→ **前段静音(volume enable)**。三者任意组合都成立。`None/0` 时不加滤镜、零开销。签名新增参数 `audio_head_mute_frames: int | None`,调用方(merger / 导出 / join-preview 经 effective_clip)透传。

## 3. 播放(`ShotPlayer` + `useShotSync`)

- `ShotPlayer` 新增 prop `headMuteSec: number | null`(来自 `audio_head_mute_sec`)。
- `useShotSync` 在 `onTimeUpdate`(已有,视频为主时钟)里:生效音轨(有 vc 用 `<audio>`,否则 `<video>`)在 `currentTime < headMuteSec` 时置 `muted=true`,越过后 `muted=false`。与现有 `trimEndSec` 钳制并列,互不干扰。
- 预览所见即所得:开头静音区无声,越过后正常。

## 4. 自动检测端点

新增只读 `POST /projects/{pid}/shots/{sid}/detect-speech-start`,镜像尾部 `detect-silence`:
- 复用现有语音检测(`video_trimmer` 里 `speech_end_info` 那套所依赖的能量/VAD 逻辑)新增 `detect_speech_start(video_path, fps) -> (sec, frame)`,找**语音起始帧**(开头静音/杂音结束、人声开始处)。
- 返回 `{ has_lead_silence: bool, suggested_start_frame: int | None, ...video_info }`。
- 只读、不计费、不改文件。走"源片视角"(`shot_source_path`,与其它裁剪只读端点一致)。

## 5. UI(裁剪弹窗波形)

`TrimDialog` / `WaveformTrack`:
- 波形轨在开头加**蓝色前手柄 + 蓝色竖线**(与尾部红色裁剪线对称);手柄**左侧**画淡蓝遮罩,表示"这之前静音"。
- 交互:拖手柄 / 点波形设定 `audio_head_mute_frames`;旁边"自动检测开头静音"按钮 → 调 `detect-speech-start` → 手柄吸到 `suggested_start_frame`。
- 帧信息行加:`前段静音: 前 N 帧 / X.XXs`(蓝色,与波形手柄同色系;N=0 时不显示)。
- 提交:新增独立端点 `PUT /projects/{pid}/shots/{sid}/audio-head-mute`,请求体
  `{ head_mute_frames: int }`(0=清除),只写 `audio_head_mute_frames`,不动 trim/vc。
  与 trim 正交、可各自保存/还原。前端拖手柄/自动检测后调它落库,并乐观更新播放器。
- 图例补一项:`蓝色前段=静音`。

## 6. 测试(遵循项目规则:不 mock 被测数据流,只 mock 计费/模型)

- 后端单测:`build_effective_clip` 加 head-mute 后,输出音频前段 RMS≈0、越过点后有声(真 ffmpeg,skip if no ffmpeg);与 trim、vc_audio 三种叠加组合各断言正确。
- `detect_speech_start`:合成"前段静音 + 后段正弦"音频,断言返回起始帧≈静音时长处。
- 端点:`detect-speech-start` 返回形状;写 `audio_head_mute_frames` 的端点落库正确、走源片视角。
- 前端 vitest:`useShotSync`/`ShotPlayer` 在 `currentTime < headMuteSec` 时生效音轨 muted、越过后取消;`WaveformTrack` 渲染前手柄 + 淡蓝遮罩 + 图例项。
- e2e:真实种子已生成 shot(带 source_fps),UI 设前段静音 + 保存 → 断言 `GET /projects/{id}` 反映 `audio_head_mute_frames`/`audio_head_mute_sec`,播放器在静音区音轨 muted。

## 非目标(YAGNI)

- 不做任意位置/多段静音(只前段一段)。
- 不做"音频前移"(那是另一种语义,已排除)。
- 不做破坏式改写音频文件(纯 EDL,渲染时应用)。
- 不改现有 trim(裁尾)/vc 的行为,仅正交新增。

## 素材文件变更审计(CLAUDE.md)

- [x] 新字段是纯 EDL 元数据,不写/不改任何素材文件(视频/音频/帧)。
- [x] 渲染时在 `build_effective_clip` 一处应用,导出/合并/join-preview 都经此,不会漏。
- [x] 只读检测端点走 `shot_source_path` 源片视角,与其它裁剪只读端点一致,不读过期派生文件。
- [x] 不新增素材命名/存储位置,无备份文件牵连,无 vc/cc 状态位牵连。
