# 首尾帧管理 + 路径单一真相重构 — 设计文档

- **日期**: 2026-06-26
- **分支**: `worktree-feat+keyframe-management`(off master)
- **状态**: 设计已确认。后端可直接进入实现计划;前端"分层下拉"UI 待 Pencil mock 后另写前端计划。

## 背景与动机

每个 shot 的视频生成依赖两张关键帧:**首帧**(起始图)和**目标尾帧**(可选,用于尾帧约束生成)。当前实现用一组状态位描述尾帧的生命周期:`tf_status`(generating/done/failed)、`tf_confirmed`(用户已确认)、`skip_tail_frame`(用户跳过)。

这套置位带来三个问题:

1. **难管理**:用户无法在分镜卡上直接增删/替换首尾帧,删除逻辑散落在多个状态分支里。
2. **regenerate 会"复活"尾帧(真 bug)**:`regenerate-shots`(`pipeline.py` ~344-366)只要发现磁盘上还有 `target_last_frame.png`,就**无条件** `tf_status="done"`、`tf_confirmed=True`;而 worker 生成判定(`tasks.py:357`)是 `if shot.tf_confirmed and shot.target_last_frame_path:`,**不看 `skip_tail_frame`**。结果:用户没要尾帧校准、甚至删过,重新生成后尾帧又被启用。
3. **置位太多易漂移**:多个标志位互相牵制,行为难推理。

## 核心思路:路径有无 = 唯一真相

**取消"用不用关键帧"的决策位,改为只判断对应路径字段是否非空且文件存在。**

| 关键帧 | 字段(沿用现有) | 判据 |
|--------|------------------|------|
| 首帧 | `custom_first_frame_path` | 非空且文件在 → 用作首帧 |
| 目标尾帧 | `target_last_frame_path` | 非空且文件在 → 尾帧约束生成;空 → 不用 |

- **添加** = 写入该路径字段(上传 / 生成 / 从本镜视频提取)。
- **删除** = 清空字段 + unlink 文件。
- `tf_confirmed` / `skip_tail_frame` **不再参与任何生成/重生成决策**(见"字段去留")。
- `tf_status` **仅保留**为尾帧异步生成的瞬时进度提示(generating / done / failed),给前端 spinner 用,**不参与决策**。

这样 regenerate 复活 bug 从根上消失:决策只看路径,删了就是 `None`,不存在"文件还在就复活"。

## 首帧初始化:显式连贯链

把现有 `_pick_first_frame`(`tasks.py:520-558`)的隐式解析链改成**预先写入 `custom_first_frame_path`**,让前端立即可见、可编辑:

- **Shot 1**:创建时 `custom_first_frame_path` ← 第一张 `character` 参考图(`_get_first_character_ref`,`tasks.py:561`)。(用户口中的"背景图" = character 参考图。)
- **连续 shot N(N>1)**:上一镜视频生成完、`last_frame_path` 提取后,自动把它写入下一镜的 `custom_first_frame_path`(隐式"上一镜尾帧→下一镜首帧"的连贯链变显式)。
- **用户覆盖**:任何时候上传/提取/删除,以路径字段为准。生成只读 `custom_first_frame_path`。

> 连贯性(哪些 shot 算"连续")仍由现有结构决定(`use_prev_last_frame` / `align_with_previous`)——这是 shot 间结构属性,不是"用不用关键帧"的决策位,保留不动。

## 生成尾帧

`generate-tail-frame`(异步)生成完后,把产物路径写入 `target_last_frame_path`、`tf_status="done"`(`tasks.py:700-701`)——已是此行为,保留。生成出来的尾帧"路径有了就会被用",**无需再单独确认**(去掉"确认尾帧"环节)。

## 端点:添加 / 删除 / 提取(上传与提取均用 ts_uuid 命名)

新增命名 helper(`storage.py`):

```python
def ts_uuid_name(ext: str = ".png") -> str:
    """时间戳_短uuid 文件名,保证每次写入唯一、天然防缓存。"""
    return f"{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
```

| 操作 | 端点 | 行为 |
|------|------|------|
| 传首帧 | `POST /projects/{pid}/shots/{sid}/upload-first-frame` | 存 `custom_frames/{ts_uuid}` → 写 `custom_first_frame_path` |
| 删首帧 | `DELETE /projects/{pid}/shots/{sid}/first-frame` | 清 `custom_first_frame_path` + unlink。生成时按连贯链/character 兜底 |
| 传尾帧 | `POST /projects/{pid}/shots/{sid}/upload-tail-frame` | 存 `shot 目录/{ts_uuid}` → 写 `target_last_frame_path`、`tf_status="done"` |
| 删尾帧 | **复用** `POST /delete-tail-frame` | 简化为:清 `target_last_frame_path` + unlink + `tf_status=None`(不再置 `skip_tail_frame`) |
| **提取本镜首帧→首帧配置** | `POST /projects/{pid}/shots/{sid}/extract-first-frame` | 把 `first_frame_path`(视频实际首帧)复制为 `custom_frames/{ts_uuid}` → 写 `custom_first_frame_path` |
| **提取本镜尾帧→尾帧配置** | `POST /projects/{pid}/shots/{sid}/extract-last-frame` | 把 `last_frame_path`(视频实际尾帧)复制为 `shot 目录/{ts_uuid}` → 写 `target_last_frame_path`、`tf_status="done"` |

> 提取 = 把"视频实际提取出的只读首/尾帧"复制(ts_uuid 命名,独立文件)回填到"可编辑的输入关键帧配置",从而替换当前分镜的首/尾帧配置。
> AI 生成的尾帧仍写固定名 `target_last_frame.png`;所有下游读 DB 字段而非硬编码名,故上传/提取用 ts_uuid 名不影响下游(见素材审计)。

## 后端改动点(精确位置,行号以 master 为准、实现时校验)

1. **`tasks.py:355-360`** — 尾帧决策改为只看路径:
   ```python
   last_frame = None
   if shot.target_last_frame_path:
       tf_path = Path(shot.target_last_frame_path)
       if tf_path.exists():
           last_frame = str(tf_path)
   ```
   去掉 `shot.tf_confirmed and` 与任何 `skip_tail_frame` 依赖。

2. **`pipeline.py` `regenerate-shots`(~344-366)** — 删除 `has_valid_tail_frame` 那段对 `tf_confirmed`/`tf_status`/`skip_tail_frame` 的翻转;`target_last_frame_path` 原样保留(用户删了即 `None`)。`tf_status` 仅在重新触发生成时置 `"generating"`。

3. **`tasks.py:304`** — `if shot.tf_confirmed and shot.motion_prompt and shot.first_frame_path:`(regenerate 复用既有参数)改为不依赖 `tf_confirmed`:以 `motion_prompt`/`first_frame_path` 是否存在为准。

4. **首帧初始化写入**:shot 1 写入 character 图路径到 `custom_first_frame_path`;一个 shot 视频生成完成、`last_frame_path` 写好后,把它写入下一连续 shot 的 `custom_first_frame_path`。

5. **`delete-tail-frame`(`pipeline.py:914-959`、`_reset_tail_frame` 51-61)** — 简化:只清 `target_last_frame_path` + `tf_status`,不再置 `skip_tail_frame`。

6. **新增端点** + `ts_uuid_name` helper:`upload-first-frame`、`upload-tail-frame`、`DELETE /first-frame`、`extract-first-frame`、`extract-last-frame`(见上表)。

## 字段去留

- `tf_status`:**保留**(瞬时进度)。
- `tf_confirmed` / `skip_tail_frame`:**从所有决策/置位逻辑中移除**;DB 列暂作休眠(本特性不做删列迁移,避免风险与范围膨胀)。验收要求:全仓搜索确认这两个字段在生成/重生成/上传/删除/提取路径中**不再被读取**。

## 前端(ShotCard)— 分层多选下拉(视觉待 Pencil mock)

把"视频尾帧"区改造为**关键帧管理控件**:一个**分层(分组) + 多选的下拉框**,用于查看/替换该分镜的首帧与尾帧配置。已确定的行为需求:

- **读取**:首帧槽读 `custom_first_frame_path`;尾帧槽读 `target_last_frame_path`;缩略图展示当前配置。
- **下拉分层(候选分组,具体布局以 Pencil mock 为准)**:
  - 「本镜提取」:提取本镜首帧 / 提取本镜尾帧 → 调 `extract-first-frame` / `extract-last-frame`。
  - 「上传」:上传首帧 / 上传尾帧 → 调 `upload-first-frame` / `upload-tail-frame`。
  - 「删除」:删首帧 / 删尾帧 → `DELETE /first-frame` / `delete-tail-frame`。
  - (多选语义、是否合并参考图多图选择等细节,待 Pencil mock 敲定。)
- 尾帧 `tf_status==='generating'` 时显示 spinner;`'failed'` 显示错误 + 重试。
- 移除原"确认尾帧"按钮/流程(生成完路径即生效)。
- 上传/提取/删除后用返回的新路径(ts_uuid 名天然带新 URL)刷新缩略图,无需手动 cache-bust。

> **前端实现计划单独写**:待 Pencil 出"分层多选下拉"mock、敲定多选语义与布局后,再据 mock 写前端 TDD 计划(调用本特性后端端点)。

## 测试

遵循项目规则:**mock 所有 AI/模型调用;不跑真 ffmpeg;Playwright mock AI 端点。**

### 后端单测
1. `ts_uuid_name`:格式 `^\d+_[0-9a-f]{8}\.png$`,两次调用不相同。
2. `upload-first-frame` / `upload-tail-frame`:文件落盘为 ts_uuid 名、对应字段写入该路径。
3. `extract-first-frame` / `extract-last-frame`:把 `first_frame_path`/`last_frame_path` 复制为 ts_uuid 文件并写入 `custom_first_frame_path`/`target_last_frame_path`;源文件不动;源缺失时报错。
4. `delete-first-frame`:清字段 + 文件删除;`delete-tail-frame`:清 `target_last_frame_path` + `tf_status`,**不再设 `skip_tail_frame`**。
5. **regenerate 不复活**(回归):`target_last_frame_path=None` → regenerate 后仍 `None`;有路径 → regenerate 后路径不变且不写决策位。
6. **worker 尾帧决策只看路径**(mock 生成器):有路径→传 last_frame;无→不传;与 `tf_confirmed` 取值无关。
7. **首帧连贯写入**:shot1 初始化为 character 图;shot N 生成后下一连续 shot 的 `custom_first_frame_path` = 本镜 `last_frame_path`。

### Playwright(前端计划阶段)
mock 各增删/提取端点,验证下拉各分层动作触发对应请求、缩略图刷新。

## 素材文件变更审计(遵循 CLAUDE.md)

- [x] 下游(generate / align-tail-frame / trim / 合并导出)读取尾帧均经 `shot.target_last_frame_path` DB 字段,非硬编码 `target_last_frame.png` → 上传/提取用 ts_uuid 名安全。
- [x] 删首/尾帧后清空对应字段 + unlink;不残留过期路径。
- [x] 提取 = 复制源文件到新 ts_uuid 文件,**不动/不删源**(`first_frame_path`/`last_frame_path` 保持只读结果)。
- [x] 首帧连贯写入只写 `custom_first_frame_path`(指向上一镜已存在的 `last_frame.png`),不复制/不删源。
- [x] ts_uuid 命名使每次写入 URL 唯一,避免前端读到缓存的旧帧。

## 非目标(YAGNI)

- 不删 `tf_confirmed` / `skip_tail_frame` 的 DB 列(休眠即可,删列迁移另议)。
- 不改连贯性结构(`use_prev_last_frame` / `align_with_previous`)。
- AI 生成尾帧暂不改为 ts_uuid 命名(仅用户上传/提取用;下游靠 DB 字段读取,无需求)。
- 不做首尾帧"对照/并排预览"视图(用户已明确不需要)。
