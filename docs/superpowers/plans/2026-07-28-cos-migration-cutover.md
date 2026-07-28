# 存量迁移与生产切换（Spec B）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把存量媒体资产搬到腾讯云 COS、回填 DB 路径字段为 key、交付孤儿对象巡检工具与切换手册，使 Spec A 的「代码只认 key」与数据实际形态在同一个窗口内对齐。

**Architecture:** 新增 `backend/app/scripts/cos_migration/` 包，拆成「纯逻辑」与「打真实 COS/DB」两层：`fields.py` 是字段登记表 + 路径→key 转换（无 IO，单元测试全覆盖，无凭证环境也能跑）；`runner.py` 实现 scan/upload/backfill/verify 四阶段；`migrate_to_cos.py` 是 argparse CLI。孤儿巡检独立为 `cos_orphan_report.py`，只读不删。四阶段全部幂等，可中断重跑。

**Tech Stack:** Python 3.12 / SQLAlchemy 2 async / aiosqlite / qcloud_cos SDK（经 Spec A 的 `app.services.object_store` 封装）/ pytest + pytest-asyncio（`asyncio_mode = "auto"`）/ uv。

---

## Global Constraints

以下为**项目级**硬约束，每个 Task 的要求都隐含包含本节。违反任何一条都应在审查阶段被打回。

- **绝不 mock**：除计费模型调用外一律不 mock。COS 打真实 dev bucket（`video-maker-dev-1414782845` / `ap-guangzhou`），DB 打真实 sqlite。禁止 fakeredis、禁止 `route.fulfill` 伪造被测数据。
- **测试 gate 判据**（有结构性守卫 `backend/tests/unit/test_cos_gating_hygiene.py` 会拦）：**带 `cos_prefix` fixture 参数的测试函数才标 `@requires_cos`；不带的一律不标。绝不加文件级 `pytestmark`。**
- **凭证隔离**：只在显式导出 `COS_SECRET_ID` / `COS_SECRET_KEY` 的 shell 里跑 `tests/integration/`。**绝不在该 shell 里跑 `tests/unit/test_cos_config.py`**——它会把凭证打进日志，本项目已因此泄露过一次 SecretId。
- **worker 侧禁止 `from app.api.*` 导入**：会在真实 vc-worker 进程里循环 import 崩溃，而测试因预先 import 了 `app.main` 完全看不见。迁移脚本同理——`app/scripts/**` 只依赖 `app.models` / `app.services` / `app.db`，**不 import 任何 `app.api.*` 或 `app.main`**。
- **禁止硬编码绝对路径**：Python 用 `pathlib` 相对 `__file__`；storage 根目录一律由 `--storage-root` 显式传入。
- **Python 工具链**：只用 `uv run --project backend`，绝不直接 `python` / `pip install`。
- **`google.genai` 必须 `vertexai=True`**（本计划不涉及，但改到相关文件时须保持）。
- **Shot 素材文件变更审计**：本计划 Task 1 删除 `pre_vc_video_key`，必须按 CLAUDE.md 的审计清单核对所有下游读取方。
- **绝不对共享 dev DB 跑 `--backfill`（Spec B §4）**：本仓所有 worktree 共用同一个 `deploy_app-data` 卷里的 `dev.db`。**一旦在它上面回填，字段就变成 key；此时把栈切回任何旧 worktree，旧代码会把 key 当路径用，媒体全部加载失败**，且不是代码能规避的。本计划全部测试一律用 `tmp_path` 临时 storage + 内存 DB（`db_session_factory` fixture 已经是 `sqlite+aiosqlite:///:memory:`），**任何任务都不需要、也不允许**把 `--backfill` 指向共享 dev DB。真正的回填只在 Task 10 的切换窗口里由人工执行。

### 本计划相对 Spec B 的四处修正（实测得出，务必遵守）

实施前已对共享 dev DB（72 projects / 139 shots / 349 个路径值）与 dev bucket 做过全量实测，Spec B 有四处与现实不符：

1. **§2.1 的幂等判据是反的，照做会让 `--backfill` 静默空转。** Spec B 说「以 `/` 开头为待转换的绝对路径，否则视为已是 key」。实测 **349 个值里 0 个以 `/` 开头**，346 个形如 `storage/projects/<id>/...`。照原判据每一行都会被判成「已经是 key」而整体跳过，媒体引用全断且 `--verify` 之前无人察觉。**判据改为按前缀正向识别**（见 Task 2）。
2. **转换不是恒等映射，而是「剥掉开头的 `storage/`」。** Spec A/B 称「本地 storage_root 相对路径 = COS key，零转换逻辑」。实际旧值是相对容器 WORKDIR `/app` 的相对路径，而 storage_root 是 `/app/storage`，因此值比 key 多一段 `storage/`。
3. **§2.2 字段清单漏了 `reference_samples.video_path` / `audio_path`**（3 行真实数据，key 落在 `analyses/` 前缀下）。已决策纳入，用**建表检测**保护（`ReferenceSample` 模型来自未合并的草稿 PR #38，本分支没有该模型，只能走原生 SQL）。因此 `--scan` / `--verify` / 孤儿巡检**都必须覆盖 `analyses/` 前缀，而不只是 `projects/`**。
4. **312 条媒体引用里 200 条的本地文件已不存在**（55 个项目目录整体消失，含 19 个 `shot_review`、2 个 `exported`）。这是迁移前就存在的破损，迁移无法修复。已决策：**照常转换、不改数据**，由 `--scan` 产出「悬空基线」，`--verify` 只对基线之外的缺失判失败（见 Task 3 / Task 6）。

### 已完成的前置动作（不要重做）

- dev bucket 的 2893 个测试孤儿对象已删除（2895 → 2，保留的 2 个属于 Spec A 上线后创建的项目 `064e3896-664e-45b8-beae-c77091d1bec3`，被 DB 真实引用）。删除前的完整清单存于 `$CLAUDE_JOB_DIR/bucket_manifest_pre_cleanup.json`。
- bucket **多版本控制未开启**（`VersioningConfiguration: None`）——属 Spec B §6 运维待办第 1 项，人工执行，不在代码范围内。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `backend/app/scripts/__init__.py` | 空包标记 |
| `backend/app/scripts/cos_migration/__init__.py` | 空包标记 |
| `backend/app/scripts/cos_migration/fields.py` | **纯逻辑**：字段登记表 `FIELDS`、`classify()`、`to_key()`、JSON 数组/字典改写。无 IO，无 COS，无 DB |
| `backend/app/scripts/cos_migration/runner.py` | 四阶段实现：`scan()` / `upload()` / `backfill()` / `verify()`，打真实 COS + DB |
| `backend/app/scripts/migrate_to_cos.py` | argparse CLI，把四阶段接到命令行 |
| `backend/app/scripts/cos_orphan_report.py` | 孤儿对象巡检，**只读不删** |
| `backend/tests/unit/test_cos_migration_fields.py` | `fields.py` 的纯单元测试（**不标 `@requires_cos`**） |
| `backend/tests/integration/test_cos_migration_flow.py` | 四阶段全流程 + 幂等 + 中断恢复 + JSON 字段 + 新列推导 |
| `backend/tests/integration/test_cos_orphan_report.py` | 巡检准确性 + **绝不删除**守卫 |
| `docs/runbooks/2026-07-28-cos-cutover.md` | 生产切换手册（Spec B §3.1 七步 + §9.2 的建列顺序陷阱） |

被修改的既有文件：`backend/app/models/project.py`、`backend/app/db.py`、`backend/worker/tasks.py`、`backend/tests/integration/conftest.py`，以及 Task 1 涉及的若干测试。被删除：`backend/app/services/vc_backup.py`。

---

## Task 1: 删除 `ensure_pre_vc_backup` 与 `pre_vc_video_key` 列

**背景（实施者必读）：** `ensure_pre_vc_backup` 会为每个变声分镜在 COS 上服务端拷贝一份**完整视频**。但 `voice-revert`（`app/api/voice.py:241`）早已改成非破坏式——`video_path` 是不可变源，VC 只另写 `vc_audio_path`，还原时只需清空 `vc_audio_path`。全仓检索确认 `pre_vc_video_key` **只被 `ensure_pre_vc_backup` 自己的幂等判断读取**，没有任何还原路径读它。这是非破坏式改造后遗留的死功能，纯浪费存储。已决策：删除创建路径**并删掉该列**。

**Files:**
- Delete: `backend/app/services/vc_backup.py`
- Modify: `backend/worker/tasks.py`（约 1055-1100 行，删 import、删调用、删随后的 `session.refresh(shot)`）
- Modify: `backend/app/models/project.py:163`
- Modify: `backend/app/db.py`（约 133 行，从建列列表移除 + 新增幂等 DROP）
- Modify: `backend/tests/integration/test_vc_cc_oss.py`（删 `ensure_pre_vc_backup` 相关用例）
- Modify: `backend/tests/integration/test_vc_nondestructive.py:72-73`
- Modify: `backend/tests/integration/test_shot_key_columns.py`

**Interfaces:**
- Produces: `Shot` 不再有 `pre_vc_video_key` 属性；`app.services.vc_backup` 模块不再存在。Task 5 的新列推导因此**只处理 `pre_cc_last_frame_key` 与 `pristine_last_frame_key` 两列**。

- [ ] **Step 1: 写失败测试——列必须消失**

在 `backend/tests/integration/test_shot_key_columns.py` 中，把现有断言改成反向断言。替换文件中所有涉及 `pre_vc_video_key` 的断言为：

```python
async def test_pre_vc_video_key_column_is_gone(db_engine):
    """pre_vc_video_key 是非破坏式改造后的死功能（没有任何还原路径读它），
    已随 ensure_pre_vc_backup 一并删除。db.py 的幂等 DROP 必须把存量库里
    的这一列也清掉。"""
    import sqlalchemy as sa
    async with db_engine.begin() as conn:
        cols = [r[1] for r in (await conn.execute(sa.text("PRAGMA table_info(shots)"))).fetchall()]
    assert "pre_vc_video_key" not in cols
    # 另外两列必须岿然不动
    assert "pre_cc_last_frame_key" in cols
    assert "pristine_last_frame_key" in cols
```

同时删除该文件里 `pre_vc_video_key` 的赋值/读取（原 28-41、49、59 行附近），以及「DROP 后重建」那个用例中对该列的引用。

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && uv run pytest tests/integration/test_shot_key_columns.py -v
```
Expected: FAIL —— `assert 'pre_vc_video_key' not in cols`（列此刻仍在）。

- [ ] **Step 3: 删除服务与调用点**

```bash
git rm backend/app/services/vc_backup.py
```

在 `backend/worker/tasks.py` 中，删除那段解释 `vc_backup` 模块位置的长注释与 `from app.services.vc_backup import ensure_pre_vc_backup`，并把：

```python
            # Idempotent server-side backup of the pre-VC video (zero local
            # traffic). ensure_pre_vc_backup opens its OWN session/transaction,
            # so refresh `shot` afterwards or the later commit on THIS session
            # would flush its stale in-memory pre_vc_video_key (None) back
            # over the just-committed value.
            await ensure_pre_vc_backup(session_factory, project_id, shot_id)
            await session.refresh(shot)

            vc_key = shot_audio_vc_key(project_id, shot_id)
```

整段替换为：

```python
            # VC 是非破坏式的：video_path 指向不可变源，只另写 vc_audio_path，
            # voice-revert 清空该指针即可还原。因此不需要备份 VC 前的整片
            # ——旧的 ensure_pre_vc_backup 备份从来没有任何读取方。
            vc_key = shot_audio_vc_key(project_id, shot_id)
```

- [ ] **Step 4: 从模型删除该列**

`backend/app/models/project.py`，删除第 163 行的 `pre_vc_video_key = Column(Text, nullable=True)` 及其上方注释。

- [ ] **Step 5: db.py —— 移出建列列表并新增幂等 DROP**

把建列循环改为只剩两列：

```python
    for col, typ in [
        ("pre_cc_last_frame_key", "TEXT"),
        ("pristine_last_frame_key", "TEXT"),
    ]:
        if not await _has_column("shots", col):
            await conn.execute(sa.text(f"ALTER TABLE shots ADD COLUMN {col} {typ}"))
```

并在其后新增幂等 DROP（照抄本文件既有的 `first_frame_path` 写法）：

```python
    # pre_vc_video_key 是非破坏式 VC 改造后的死功能：写入方 ensure_pre_vc_backup
    # 已删除，且从来没有任何还原路径读它（voice-revert 只清 vc_audio_path）。
    # 存量库里可能已建出该列，这里幂等删掉。
    if await _has_column("shots", "pre_vc_video_key"):
        await conn.execute(sa.text("ALTER TABLE shots DROP COLUMN pre_vc_video_key"))
```

- [ ] **Step 6: 清理其余测试引用**

`backend/tests/integration/test_vc_cc_oss.py`：删除整个 `ensure_pre_vc_backup` 用例（约 121-150 行，含那段解释「反例对照」的注释）以及 211-213 行的两条断言。
`backend/tests/integration/test_vc_nondestructive.py`：删除 72-73 行两条断言。

删完后确认全仓无残留：

```bash
grep -rn "pre_vc_video_key\|ensure_pre_vc_backup\|vc_backup" backend/app backend/worker backend/tests --include=*.py
```
Expected: 无输出。

- [ ] **Step 7: 运行测试确认通过**

```bash
cd backend
export COS_SECRET_ID=$(cat ../deploy/secrets/cos_secret_id)
export COS_SECRET_KEY=$(cat ../deploy/secrets/cos_secret_key)
uv run pytest tests/integration/test_shot_key_columns.py tests/integration/test_vc_cc_oss.py tests/integration/test_vc_nondestructive.py tests/unit/test_cos_gating_hygiene.py -v
```
Expected: PASS（`test_pre_vc_video_key_column_is_gone` 绿）。
**注意：这个 shell 已导出凭证，绝不要在其中跑 `tests/unit/test_cos_config.py`。**

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor(vc): 删除死功能 ensure_pre_vc_backup 与 pre_vc_video_key 列

voice-revert 早已非破坏式（video_path 不可变，只清 vc_audio_path），
该备份路径为每个变声分镜白拷一份整片却从无读取方。"
```

---

## Task 2: `fields.py` —— 字段登记表与路径→key 转换（纯逻辑）

**Files:**
- Create: `backend/app/scripts/__init__.py`（空文件）
- Create: `backend/app/scripts/cos_migration/__init__.py`（空文件）
- Create: `backend/app/scripts/cos_migration/fields.py`
- Test: `backend/tests/unit/test_cos_migration_fields.py`

**Interfaces:**
- Produces: `FieldSpec(table, column, pk, kind, optional_table)`；常量 `FIELDS: list[FieldSpec]`；`classify(value) -> str` 返回 `ALREADY_KEY|LEGACY_REL|LEGACY_ABS|UNRECOGNIZED` 之一；`to_key(value) -> str`；`rewrite_json_list(raw) -> str | None`；`rewrite_json_dict_of_lists(raw) -> str | None`（返回 `None` 表示无变化）。Task 3/5/6 全部消费这些。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/unit/test_cos_migration_fields.py`。**本文件不带 `cos_prefix`，因此绝不标 `@requires_cos`**（结构性守卫会拦）。

```python
"""fields.py 的纯逻辑测试——不碰 COS、不碰 DB，无凭证环境必须照跑。"""
import json

import pytest

from app.scripts.cos_migration.fields import (
    ALREADY_KEY, LEGACY_ABS, LEGACY_REL, UNRECOGNIZED,
    FIELDS, classify, rewrite_json_dict_of_lists, rewrite_json_list, to_key,
)


@pytest.mark.parametrize("value,expected", [
    # 真实 dev DB 里 346/349 个值长这样——注意它不以 / 开头
    ("storage/projects/abc/shots/shot_1/output.mp4", LEGACY_REL),
    ("storage/analyses/xyz/samples/1/source.mp4", LEGACY_REL),
    # Spec A 上线后新写入的值已经是 key
    ("projects/abc/storyboard.json", ALREADY_KEY),
    ("analyses/xyz/samples/1/source.mp4", ALREADY_KEY),
    # 防御性：万一某个库里存的是绝对路径
    ("/app/storage/projects/abc/shots/shot_1/output.mp4", LEGACY_ABS),
    # 认不出来的一律不碰
    ("", UNRECOGNIZED),
    ("some/other/thing.png", UNRECOGNIZED),
    ("/etc/passwd", UNRECOGNIZED),
])
def test_classify(value, expected):
    assert classify(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("storage/projects/abc/shots/shot_1/output.mp4", "projects/abc/shots/shot_1/output.mp4"),
    ("storage/analyses/xyz/samples/1/source.mp4", "analyses/xyz/samples/1/source.mp4"),
    ("/app/storage/projects/abc/x.png", "projects/abc/x.png"),
    # 幂等：已经是 key 的原样返回
    ("projects/abc/storyboard.json", "projects/abc/storyboard.json"),
])
def test_to_key(value, expected):
    assert to_key(value) == expected


def test_to_key_is_idempotent():
    """连跑两次必须收敛——这是 Spec B §2.1 的验收标准在纯逻辑层的体现。"""
    once = to_key("storage/projects/abc/shots/shot_1/output.mp4")
    assert to_key(once) == once


def test_to_key_rejects_unrecognized():
    with pytest.raises(ValueError):
        to_key("some/other/thing.png")


def test_rewrite_json_list():
    raw = json.dumps([
        "storage/projects/p/shots/shot_3/custom_frames/a.jpg",
        "storage/projects/p/shots/shot_3/custom_frames/b.jpg",
    ])
    out = rewrite_json_list(raw)
    assert json.loads(out) == [
        "projects/p/shots/shot_3/custom_frames/a.jpg",
        "projects/p/shots/shot_3/custom_frames/b.jpg",
    ]
    # 幂等：已转换过的返回 None（无变化）
    assert rewrite_json_list(out) is None


def test_rewrite_json_dict_of_lists():
    raw = json.dumps({
        "character": ["storage/projects/p/reference_images/c.jpg"],
        "object": [],
    })
    out = rewrite_json_dict_of_lists(raw)
    assert json.loads(out) == {
        "character": ["projects/p/reference_images/c.jpg"],
        "object": [],
    }
    assert rewrite_json_dict_of_lists(out) is None


def test_rewrite_json_handles_empty_and_garbage():
    for raw in ("", "[]", "null", "not json"):
        assert rewrite_json_list(raw) is None


def test_field_registry_covers_every_known_path_column():
    """字段漏一个 = 该类资源全部失效（Spec B §9.1）。这里把实测确认过的
    完整清单钉死，将来加字段必须同步改这个断言。"""
    got = {(f.table, f.column) for f in FIELDS}
    assert got == {
        ("shots", "video_path"),
        ("shots", "last_frame_path"),
        ("shots", "custom_first_frame_path"),
        ("shots", "target_last_frame_path"),
        ("shots", "vc_audio_path"),
        ("shots", "custom_reference_paths"),
        ("projects", "storyboard_path"),
        ("projects", "final_video_path"),
        ("projects", "reference_voice_path"),
        ("reference_images", "storage_path"),
        ("image_candidates", "file_path"),
        ("image_candidates", "ref_paths"),
        ("reference_samples", "video_path"),
        ("reference_samples", "audio_path"),
    }


def test_reference_samples_is_marked_optional():
    """ReferenceSample 模型来自未合并的草稿 PR #38，本分支没有——
    必须靠建表检测保护，不能硬 import。"""
    opt = {(f.table, f.column) for f in FIELDS if f.optional_table}
    assert opt == {("reference_samples", "video_path"), ("reference_samples", "audio_path")}


def test_json_fields_are_marked_with_right_kind():
    kinds = {(f.table, f.column): f.kind for f in FIELDS}
    assert kinds[("shots", "custom_reference_paths")] == "json_list"
    assert kinds[("image_candidates", "ref_paths")] == "json_dict_of_lists"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && uv run pytest tests/unit/test_cos_migration_fields.py -v
```
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.scripts'`。

- [ ] **Step 3: 实现 `fields.py`**

```bash
mkdir -p backend/app/scripts/cos_migration
touch backend/app/scripts/__init__.py backend/app/scripts/cos_migration/__init__.py
```

创建 `backend/app/scripts/cos_migration/fields.py`：

```python
"""存量迁移的字段登记表与「本地路径 → COS key」转换。

纯逻辑：不碰 COS、不碰 DB，因此可以在无凭证环境下用单元测试全量覆盖。

为什么判据不是 Spec B §2.1 写的「以 / 开头为待转换的绝对路径」——
实测共享 dev DB 的 349 个路径值里**一个都不以 / 开头**，346 个形如
``storage/projects/<id>/...``：旧代码存的是相对容器 WORKDIR ``/app`` 的
相对路径，而 storage_root 是 ``/app/storage``，所以值比 key 多一段
``storage/``。照 Spec B 的判据每一行都会被判成「已经是 key」而整体跳过，
--backfill 变成静默空转、媒体引用全断，且直到 --verify 之前无人察觉。
因此判据改成**按前缀正向识别**：只有明确认得出的形态才转换，认不出的
一律原样保留并单独报告，绝不猜。
"""

import json
from dataclasses import dataclass

# COS key 的合法顶层前缀。projects/ 是分镜与项目素材，analyses/ 是
# 内容分析的参考样本（reference_samples，Spec B §2.2 漏掉的那一组）。
KEY_PREFIXES = ("projects/", "analyses/")

# 旧值相对 /app 的前缀
LEGACY_REL_PREFIX = "storage/"

# 绝对路径里定位 storage 根的分隔片段
_ABS_MARKER = "/storage/"

ALREADY_KEY = "already_key"
LEGACY_REL = "legacy_relative"
LEGACY_ABS = "legacy_absolute"
UNRECOGNIZED = "unrecognized"


def classify(value: str) -> str:
    """判断一个 DB 路径值的形态。空值/认不出的返回 UNRECOGNIZED。"""
    if not value:
        return UNRECOGNIZED
    if value.startswith(KEY_PREFIXES):
        return ALREADY_KEY
    if value.startswith(LEGACY_REL_PREFIX):
        return LEGACY_REL
    if value.startswith("/") and _ABS_MARKER in value:
        return LEGACY_ABS
    return UNRECOGNIZED


def to_key(value: str) -> str:
    """把 DB 路径值转成 COS key。已是 key 的原样返回（幂等）。

    认不出形态时抛 ValueError —— 宁可让调用方把它记进报告里等人看，
    也不猜着改数据。
    """
    kind = classify(value)
    if kind == ALREADY_KEY:
        return value
    if kind == LEGACY_REL:
        return value[len(LEGACY_REL_PREFIX):]
    if kind == LEGACY_ABS:
        return value.split(_ABS_MARKER, 1)[1]
    raise ValueError(f"unrecognized path value: {value!r}")


def _convert_items(items: list) -> tuple[list, int]:
    """转换字符串列表，返回 (新列表, 变更数)。非字符串项原样保留。"""
    out, changed = [], 0
    for it in items:
        if isinstance(it, str) and classify(it) in (LEGACY_REL, LEGACY_ABS):
            out.append(to_key(it))
            changed += 1
        else:
            out.append(it)
    return out, changed


def rewrite_json_list(raw: str) -> str | None:
    """改写 JSON 数组字段（shots.custom_reference_paths）。

    返回新的 JSON 文本；无变化（含空值/非法 JSON/结构不符）时返回 None。
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    out, changed = _convert_items(data)
    return json.dumps(out, ensure_ascii=False) if changed else None


def rewrite_json_dict_of_lists(raw: str) -> str | None:
    """改写「字典套数组」字段（image_candidates.ref_paths，形如
    {"character": [...], "object": [...]}）。语义同 rewrite_json_list。
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    out, total = {}, 0
    for k, v in data.items():
        if isinstance(v, list):
            out[k], changed = _convert_items(v)
            total += changed
        else:
            out[k] = v
    return json.dumps(out, ensure_ascii=False) if total else None


@dataclass(frozen=True)
class FieldSpec:
    """一个需要回填的 DB 字段。

    kind: "scalar" | "json_list" | "json_dict_of_lists"
    optional_table: 表可能不存在（未合并的草稿 PR #38 带来的
        reference_samples）——处理前必须先查表是否存在。
    """
    table: str
    column: str
    pk: str
    kind: str
    optional_table: bool = False


# 完整清单。来自对共享 dev DB 全部 TEXT/VARCHAR 列逐列取样的实测，
# 而非照抄 Spec B §2.2（它漏了 reference_samples 两列）。
# 漏一个字段 = 该类资源全部失效，新增字段务必同步更新
# tests/unit/test_cos_migration_fields.py 里钉死的断言。
FIELDS: list[FieldSpec] = [
    FieldSpec("shots", "video_path", "id", "scalar"),
    FieldSpec("shots", "last_frame_path", "id", "scalar"),
    FieldSpec("shots", "custom_first_frame_path", "id", "scalar"),
    FieldSpec("shots", "target_last_frame_path", "id", "scalar"),
    FieldSpec("shots", "vc_audio_path", "id", "scalar"),
    FieldSpec("shots", "custom_reference_paths", "id", "json_list"),
    FieldSpec("projects", "storyboard_path", "id", "scalar"),
    FieldSpec("projects", "final_video_path", "id", "scalar"),
    FieldSpec("projects", "reference_voice_path", "id", "scalar"),
    FieldSpec("reference_images", "storage_path", "id", "scalar"),
    FieldSpec("image_candidates", "file_path", "id", "scalar"),
    FieldSpec("image_candidates", "ref_paths", "id", "json_dict_of_lists"),
    FieldSpec("reference_samples", "video_path", "id", "scalar", optional_table=True),
    FieldSpec("reference_samples", "audio_path", "id", "scalar", optional_table=True),
]
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && uv run pytest tests/unit/test_cos_migration_fields.py -v
```
Expected: PASS（全部用例）。

- [ ] **Step 5: 确认 gate 守卫仍绿**

```bash
cd backend && uv run pytest tests/unit/test_cos_gating_hygiene.py -v
```
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add backend/app/scripts backend/tests/unit/test_cos_migration_fields.py
git commit -m "feat(migrate): 字段登记表与路径→key 转换（纯逻辑）

判据按前缀正向识别，修掉 Spec B §2.1 反向判据会让 backfill 静默空转的问题；
字段清单据实测补上 Spec B 漏掉的 reference_samples 两列。"
```

---

## Task 3: `--scan` —— 清单、悬空基线与未引用文件

**Files:**
- Create: `backend/app/scripts/cos_migration/runner.py`
- Test: `backend/tests/integration/test_cos_migration_flow.py`（本任务只加 scan 用例）

**Interfaces:**
- Consumes: Task 2 的 `FIELDS` / `classify` / `to_key` / `rewrite_json_*`。
- Produces:
  - `async def collect_db_refs(session_factory) -> list[DbRef]`，`DbRef(table, column, pk, raw, key, kind)`（`key` 在 `UNRECOGNIZED` 时为 `None`）
  - `async def scan(storage_root: Path, session_factory) -> dict`，返回并可序列化为报告
  - `def write_report(report: dict, report_dir: Path, name: str) -> Path`
  - 报告 schema（Task 4/6 依赖）：
    ```json
    {"phase": "scan",
     "local": {"files": 0, "bytes": 0},
     "db": {"total": 0, "already_key": 0, "legacy_relative": 0,
            "legacy_absolute": 0, "unrecognized": 0},
     "dangling": [{"table": "...", "column": "...", "pk": "...",
                   "raw": "...", "key": "..."}],
     "unreferenced_local": {"files": 0, "bytes": 0, "sample": ["..."]},
     "unrecognized": [{"table": "...", "column": "...", "pk": "...", "raw": "..."}]}
    ```

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/integration/test_cos_migration_flow.py`。注意：**本任务的两个用例都不需要真实 COS**（scan 只读本地磁盘与 DB），所以**不带 `cos_prefix`、不标 `@requires_cos`**。

```python
"""迁移脚本四阶段的集成测试。

分工约定（结构性守卫 tests/unit/test_cos_gating_hygiene.py 会拦）：
只有真正要打 COS 的用例才带 cos_prefix 参数并加 @requires_cos；
scan/backfill 这种只碰本地磁盘与 DB 的用例一律不标，好让无凭证环境照跑。
"""
import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.scripts.cos_migration.runner import collect_db_refs, scan
from tests.integration.conftest_cos import requires_cos


async def _seed_legacy_rows(sf, storage_root: Path):
    """造一个「迁移前」的库：路径值都是 storage/... 相对路径。

    只写 DB + 本地文件，不碰 COS —— 这正是切换窗口第 5 步之前的真实形态。
    返回 (project_id, shot_dir)。
    """
    pid = "11111111-1111-1111-1111-111111111111"
    shot_dir = storage_root / "projects" / pid / "shots" / "shot_1"
    shot_dir.mkdir(parents=True)
    (shot_dir / "output.mp4").write_bytes(b"video-bytes")
    (shot_dir / "last_frame.png").write_bytes(b"png-bytes")
    (storage_root / "projects" / pid / "storyboard.json").write_text("{}")

    async with sf() as s:
        await s.execute(sa.text(
            "INSERT INTO projects (id,title,theme_text,creator_name,status,aspect_ratio,storyboard_path) "
            "VALUES (:i,'t','t','a','draft','9:16',:sb)"),
            {"i": pid, "sb": f"storage/projects/{pid}/storyboard.json"})
        await s.execute(sa.text(
            "INSERT INTO shots (project_id,shot_id,text,shot_type,visual_description,status,"
            "video_path,last_frame_path) VALUES (:p,1,'t','Wide','v','completed',:v,:l)"),
            {"p": pid,
             "v": f"storage/projects/{pid}/shots/shot_1/output.mp4",
             "l": f"storage/projects/{pid}/shots/shot_1/last_frame.png"})
        await s.commit()
    return pid, shot_dir


async def test_collect_db_refs_classifies_legacy_relative_values(db_session_factory, tmp_path):
    pid, _ = await _seed_legacy_rows(db_session_factory, tmp_path)
    refs = await collect_db_refs(db_session_factory)
    by_col = {(r.table, r.column): r for r in refs}
    assert by_col[("shots", "video_path")].key == f"projects/{pid}/shots/shot_1/output.mp4"
    assert by_col[("projects", "storyboard_path")].key == f"projects/{pid}/storyboard.json"
    # 全部应被判为待转换，绝不能被判成「已经是 key」
    assert all(r.kind == "legacy_relative" for r in refs)


async def test_scan_reports_dangling_and_unreferenced(db_session_factory, tmp_path):
    pid, shot_dir = await _seed_legacy_rows(db_session_factory, tmp_path)
    # 制造一条悬空引用：DB 有 last_frame_path，但把文件删掉
    (shot_dir / "last_frame.png").unlink()
    # 制造一个未被引用的本地文件
    (shot_dir / "leftover.png").write_bytes(b"x")

    report = await scan(tmp_path, db_session_factory)

    assert report["db"]["legacy_relative"] == 3
    assert report["db"]["already_key"] == 0
    dangling_keys = {d["key"] for d in report["dangling"]}
    assert dangling_keys == {f"projects/{pid}/shots/shot_1/last_frame.png"}
    assert report["unreferenced_local"]["files"] == 1
    assert report["local"]["files"] == 3   # output.mp4 + storyboard.json + leftover.png
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && uv run pytest tests/integration/test_cos_migration_flow.py -v
```
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.scripts.cos_migration.runner'`。

- [ ] **Step 3: 实现 `runner.py` 的 scan 部分**

创建 `backend/app/scripts/cos_migration/runner.py`：

```python
"""存量迁移四阶段：scan / upload / backfill / verify。

设计要点：
- 四阶段全部幂等，可中断重跑（Spec B §2.1 的硬要求：连跑两次，
  第二次变更数必须为 0）。
- ``key_prefix`` 在**对象存储边界**统一生效：生产用 ""，测试传
  cos_prefix，好让集成测试写进独立前缀、teardown 能删干净。DB 里存的
  永远是不带前缀的 key。历史上正是因为测试直接写真实 projects/ 前缀
  且无 teardown，dev bucket 才积了 2893 个孤儿对象。
- 本模块只依赖 app.models / app.services / app.db，**绝不 import
  app.api.* 或 app.main**（worker 侧同源禁忌，见 CLAUDE.md）。
"""

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import sqlalchemy as sa

from app.scripts.cos_migration.fields import (
    ALREADY_KEY, LEGACY_ABS, LEGACY_REL, UNRECOGNIZED,
    FIELDS, classify, rewrite_json_dict_of_lists, rewrite_json_list, to_key,
)

logger = logging.getLogger(__name__)


@dataclass
class DbRef:
    table: str
    column: str
    pk: str
    raw: str
    key: Optional[str]
    kind: str


async def _table_exists(conn, table: str) -> bool:
    r = await conn.execute(sa.text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=:t"), {"t": table})
    return r.first() is not None


def _refs_from_value(spec, pk, raw: str) -> list[DbRef]:
    """把一个字段值展开成 0..n 条引用（JSON 字段会展开成多条）。"""
    out = []
    if spec.kind == "scalar":
        kind = classify(raw)
        key = to_key(raw) if kind != UNRECOGNIZED else None
        out.append(DbRef(spec.table, spec.column, str(pk), raw, key, kind))
        return out
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return out
    items = []
    if spec.kind == "json_list" and isinstance(data, list):
        items = [x for x in data if isinstance(x, str)]
    elif spec.kind == "json_dict_of_lists" and isinstance(data, dict):
        items = [x for v in data.values() if isinstance(v, list) for x in v if isinstance(x, str)]
    for it in items:
        kind = classify(it)
        key = to_key(it) if kind != UNRECOGNIZED else None
        out.append(DbRef(spec.table, spec.column, str(pk), it, key, kind))
    return out


async def collect_db_refs(session_factory) -> list[DbRef]:
    """遍历 FIELDS，收集全部非空路径值并分类。"""
    refs: list[DbRef] = []
    async with session_factory() as s:
        conn = await s.connection()
        for spec in FIELDS:
            if spec.optional_table and not await _table_exists(conn, spec.table):
                logger.info("skip_missing_table", extra={"table": spec.table})
                continue
            rows = await s.execute(sa.text(
                f"SELECT {spec.pk}, {spec.column} FROM {spec.table} "
                f"WHERE {spec.column} IS NOT NULL AND {spec.column} <> ''"))
            for pk, raw in rows.fetchall():
                refs.extend(_refs_from_value(spec, pk, raw))
    return refs


def _walk_local(storage_root: Path) -> dict[str, int]:
    """storage_root 下所有文件 → {相对路径(=key): 字节数}。"""
    out = {}
    for p in storage_root.rglob("*"):
        if p.is_file():
            out[p.relative_to(storage_root).as_posix()] = p.stat().st_size
    return out


async def scan(storage_root: Path, session_factory) -> dict:
    """扫描本地 storage 与 DB，产出迁移清单 + 悬空基线 + 未引用文件报告。

    悬空基线是 Spec B 没有的概念，但现实需要：实测 312 条媒体引用里有
    200 条的本地文件早已不存在（55 个项目目录整体消失）。这是迁移**之前**
    就存在的破损，迁移无法修复。把它们固化成基线，--verify 才能对
    「基线之外的缺失」判失败而不是永远飘红。
    """
    storage_root = Path(storage_root)
    local = _walk_local(storage_root)
    refs = await collect_db_refs(session_factory)

    counts = {ALREADY_KEY: 0, LEGACY_REL: 0, LEGACY_ABS: 0, UNRECOGNIZED: 0}
    dangling, unrecognized, referenced = [], [], set()
    for r in refs:
        counts[r.kind] += 1
        if r.key is None:
            unrecognized.append({"table": r.table, "column": r.column,
                                 "pk": r.pk, "raw": r.raw})
            continue
        referenced.add(r.key)
        if r.key not in local:
            dangling.append(asdict(r))

    unref = {k: v for k, v in local.items() if k not in referenced}
    return {
        "phase": "scan",
        "local": {"files": len(local), "bytes": sum(local.values())},
        "db": {"total": len(refs), **{k: counts[k] for k in counts}},
        "dangling": dangling,
        "unreferenced_local": {
            "files": len(unref), "bytes": sum(unref.values()),
            "sample": sorted(unref)[:50],
        },
        "unrecognized": unrecognized,
    }


def write_report(report: dict, report_dir: Path, name: str) -> Path:
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{name}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && uv run pytest tests/integration/test_cos_migration_flow.py -v
```
Expected: PASS（2 个用例）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/scripts/cos_migration/runner.py backend/tests/integration/test_cos_migration_flow.py
git commit -m "feat(migrate): --scan 阶段，产出迁移清单/悬空基线/未引用文件报告"
```

---

## Task 4: `--upload` —— 幂等上传与中断恢复

**Files:**
- Modify: `backend/app/scripts/cos_migration/runner.py`（追加 `upload()`）
- Test: `backend/tests/integration/test_cos_migration_flow.py`（追加 upload 用例）

**Interfaces:**
- Consumes: Task 3 的 `_walk_local`；`app.services.object_store` 的 `put` / `size`。
- Produces: `async def upload(storage_root: Path, key_prefix: str = "", only: list[str] | None = None) -> dict`，返回 `{"phase":"upload","uploaded":n,"skipped":n,"failed":[{"key":...,"error":...}],"bytes":n}`。

**为什么用大小比对而非 CRC64：** COS 的简单上传是原子的——中断不会留下半截对象；分块上传的未完成分片不会出现在 `list_objects` 里。因此「对象存在且字节数一致」已足以判定该文件无需重传。省掉 CRC64 可以不改 Spec A 的 `object_store`（它只暴露 `size()`，不暴露 head 原始响应）。

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/integration/test_cos_migration_flow.py`。**这些用例打真实 COS，必须带 `cos_prefix` 且标 `@requires_cos`。**

```python
@requires_cos
async def test_upload_is_idempotent_and_resumable(db_session_factory, tmp_path, cos_prefix):
    """Spec B §2.1 的验收标准：连跑两次，第二次变更数必须为 0。
    并且中断后重跑只补差量。"""
    from app.services import object_store

    pid, shot_dir = await _seed_legacy_rows(db_session_factory, tmp_path)

    # 模拟「上传到一半中断」：先只传其中一个文件
    first = await upload(tmp_path, key_prefix=cos_prefix,
                         only=[f"projects/{pid}/shots/shot_1/output.mp4"])
    assert first["uploaded"] == 1

    # 补跑全量：只应补上剩下的两个，已传的那个被跳过
    second = await upload(tmp_path, key_prefix=cos_prefix)
    assert second["uploaded"] == 2
    assert second["skipped"] == 1
    assert second["failed"] == []

    # 第三次：全部跳过，变更数为 0
    third = await upload(tmp_path, key_prefix=cos_prefix)
    assert third["uploaded"] == 0
    assert third["skipped"] == 3

    # 真实校验对象确实在 COS 上，且内容正确
    assert await object_store.exists(f"{cos_prefix}projects/{pid}/shots/shot_1/output.mp4")
    dest = tmp_path / "roundtrip.mp4"
    await object_store.get(f"{cos_prefix}projects/{pid}/shots/shot_1/output.mp4", dest)
    assert dest.read_bytes() == b"video-bytes"


@requires_cos
async def test_upload_reuploads_when_size_differs(db_session_factory, tmp_path, cos_prefix):
    """大小不一致说明本地文件在停服前又被改过，必须重传而不是跳过。"""
    pid, shot_dir = await _seed_legacy_rows(db_session_factory, tmp_path)
    await upload(tmp_path, key_prefix=cos_prefix)

    (shot_dir / "output.mp4").write_bytes(b"video-bytes-but-longer")
    again = await upload(tmp_path, key_prefix=cos_prefix)
    assert again["uploaded"] == 1
    assert again["skipped"] == 2
```

在文件顶部的 import 里加上 `upload`：

```python
from app.scripts.cos_migration.runner import collect_db_refs, scan, upload
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend
export COS_SECRET_ID=$(cat ../deploy/secrets/cos_secret_id)
export COS_SECRET_KEY=$(cat ../deploy/secrets/cos_secret_key)
uv run pytest tests/integration/test_cos_migration_flow.py -v
```
Expected: FAIL —— `ImportError: cannot import name 'upload'`。

- [ ] **Step 3: 实现 `upload()`**

在 `runner.py` 顶部 import 区加入：

```python
from app.services import cos_client, object_store
```

并追加：

```python
async def upload(storage_root: Path, key_prefix: str = "",
                 only: Optional[list[str]] = None) -> dict:
    """把本地 storage 下的文件传到 COS。幂等：已存在且字节数一致则跳过。

    ``only`` 限定只处理给定的 key 列表（用于按失败清单重试，以及测试里
    模拟「传到一半中断」）。

    可在线运行、不动 DB —— 这是切换窗口能压到极短的原因（Spec B §3.1
    第 2 步在线消化掉大头）。
    """
    storage_root = Path(storage_root)
    await cos_client.warm_credentials()
    local = _walk_local(storage_root)
    if only is not None:
        wanted = set(only)
        local = {k: v for k, v in local.items() if k in wanted}

    uploaded = skipped = sent_bytes = 0
    failed = []
    for key in sorted(local):
        n = local[key]
        remote = f"{key_prefix}{key}"
        try:
            if await object_store.exists(remote) and await object_store.size(remote) == n:
                skipped += 1
                continue
            await object_store.put(remote, storage_root / key)
            uploaded += 1
            sent_bytes += n
        except Exception as e:  # 单个文件失败不中断整体，记进报告供重试
            logger.error("migrate_upload_failed", extra={"key": remote, "error": repr(e)})
            failed.append({"key": key, "error": repr(e)})
    return {"phase": "upload", "uploaded": uploaded, "skipped": skipped,
            "failed": failed, "bytes": sent_bytes}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend
export COS_SECRET_ID=$(cat ../deploy/secrets/cos_secret_id)
export COS_SECRET_KEY=$(cat ../deploy/secrets/cos_secret_key)
uv run pytest tests/integration/test_cos_migration_flow.py -v
```
Expected: PASS（4 个用例）。

- [ ] **Step 5: 确认 gate 守卫仍绿，且 dev bucket 没有新增 projects/ 孤儿**

```bash
cd backend && uv run pytest tests/unit/test_cos_gating_hygiene.py -v
```
Expected: PASS。测试写入的对象都在 `test/<uuid>/` 前缀下，由 `cos_prefix` teardown 删除。

- [ ] **Step 6: Commit**

```bash
git add backend/app/scripts/cos_migration/runner.py backend/tests/integration/test_cos_migration_flow.py
git commit -m "feat(migrate): --upload 幂等上传，支持中断恢复与按失败清单重试"
```

---

## Task 5: `--backfill` —— 回填 key 与推导两个新列

**Files:**
- Modify: `backend/app/scripts/cos_migration/runner.py`（追加 `backfill()`）
- Test: `backend/tests/integration/test_cos_migration_flow.py`（追加 backfill 用例）

**Interfaces:**
- Consumes: Task 2 的 `FIELDS` / `rewrite_json_*`；`app.db` 的建表建列例程。
- Produces: `async def backfill(storage_root: Path, session_factory) -> dict`，返回 `{"phase":"backfill","changed":n,"skipped":n,"unrecognized":[...],"derived":{"pre_cc_last_frame_key":n,"pristine_last_frame_key":n}}`。

**两处关键约束：**
1. **必须先建列再回填**（Spec B §9.2）。本项目不用 alembic，两个 key 列由 `app/db.py` 的幂等 `ALTER TABLE` 在应用启动时创建；而切换顺序里 `--backfill`（第 5 步）**早于**部署新代码首次启动（第 6 步）。因此 `backfill()` 自己先调一次建列例程。
2. **两个新列的初值只能在本地文件尚存时扫描推导，事后无法补做**（Spec B §2.3）。`pre_vc_video_key` 已在 Task 1 删除，**不再推导**。

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/integration/test_cos_migration_flow.py`。**这些用例只碰本地磁盘与 DB，不带 `cos_prefix`、不标 `@requires_cos`。**

```python
async def test_backfill_converts_scalar_and_json_fields_idempotently(db_session_factory, tmp_path):
    pid, _ = await _seed_legacy_rows(db_session_factory, tmp_path)
    # JSON 数组字段：Spec B 点名最容易遗漏的地方
    async with db_session_factory() as s:
        await s.execute(sa.text(
            "UPDATE shots SET custom_reference_paths = :v WHERE project_id = :p"),
            {"p": pid, "v": json.dumps([
                f"storage/projects/{pid}/shots/shot_1/custom_frames/a.jpg",
                f"storage/projects/{pid}/shots/shot_1/custom_frames/b.jpg"])})
        await s.commit()

    first = await backfill(tmp_path, db_session_factory)
    assert first["changed"] > 0

    async with db_session_factory() as s:
        row = (await s.execute(sa.text(
            "SELECT video_path, custom_reference_paths FROM shots WHERE project_id = :p"),
            {"p": pid})).first()
    assert row[0] == f"projects/{pid}/shots/shot_1/output.mp4"
    assert json.loads(row[1]) == [
        f"projects/{pid}/shots/shot_1/custom_frames/a.jpg",
        f"projects/{pid}/shots/shot_1/custom_frames/b.jpg"]

    # Spec B §2.1 验收标准：第二次变更数必须为 0
    second = await backfill(tmp_path, db_session_factory)
    assert second["changed"] == 0


async def test_backfill_derives_new_key_columns(db_session_factory, tmp_path):
    """两个新列的初值只能在本地文件尚存时推导，事后无法补做（Spec B §2.3）。
    pristine 取 last_frame_*.png 中排除固定名备份后 mtime 最新的那个。"""
    import os, time
    pid, shot_dir = await _seed_legacy_rows(db_session_factory, tmp_path)
    (shot_dir / "last_frame_pre_cc.png").write_bytes(b"pre-cc")
    (shot_dir / "last_frame_1700000000_aaaa.png").write_bytes(b"older")
    (shot_dir / "last_frame_1800000000_bbbb.png").write_bytes(b"newest")
    now = time.time()
    os.utime(shot_dir / "last_frame_1700000000_aaaa.png", (now - 500, now - 500))
    os.utime(shot_dir / "last_frame_1800000000_bbbb.png", (now, now))

    await backfill(tmp_path, db_session_factory)

    async with db_session_factory() as s:
        row = (await s.execute(sa.text(
            "SELECT pre_cc_last_frame_key, pristine_last_frame_key FROM shots "
            "WHERE project_id = :p"), {"p": pid})).first()
    assert row[0] == f"projects/{pid}/shots/shot_1/last_frame_pre_cc.png"
    assert row[1] == f"projects/{pid}/shots/shot_1/last_frame_1800000000_bbbb.png"


async def test_backfill_leaves_unrecognized_values_untouched(db_session_factory, tmp_path):
    """认不出形态的值绝不猜着改，只记进报告。"""
    pid, _ = await _seed_legacy_rows(db_session_factory, tmp_path)
    async with db_session_factory() as s:
        await s.execute(sa.text(
            "UPDATE projects SET final_video_path = 'weird/thing.mp4' WHERE id = :p"),
            {"p": pid})
        await s.commit()

    report = await backfill(tmp_path, db_session_factory)

    async with db_session_factory() as s:
        v = (await s.execute(sa.text(
            "SELECT final_video_path FROM projects WHERE id = :p"), {"p": pid})).scalar_one()
    assert v == "weird/thing.mp4"
    assert any(u["raw"] == "weird/thing.mp4" for u in report["unrecognized"])
```

import 行更新为：

```python
from app.scripts.cos_migration.runner import backfill, collect_db_refs, scan, upload
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && uv run pytest tests/integration/test_cos_migration_flow.py -k backfill -v
```
Expected: FAIL —— `ImportError: cannot import name 'backfill'`。

- [ ] **Step 3: 实现 `backfill()`**

追加到 `runner.py`：

```python
async def _derive_key_columns(storage_root: Path, session_factory) -> dict:
    """推导 pre_cc_last_frame_key / pristine_last_frame_key 的初值。

    只在本地文件尚存时能扫出来，**事后永远无法补做**（Spec B §2.3）：
    本地目录是这些信息的唯一来源，填错的后果是 CC 还原链路对存量分镜失效。

    这里自行实现目录扫描，不 import 旧的 pristine_last_frame_path() ——
    Spec A 阶段 5 已把那些本地路径函数删掉了，且脚本只在切换窗口跑一次，
    自包含反而更清晰。

    只填当前为 NULL 的行，因此重跑变更数为 0。
    """
    storage_root = Path(storage_root)
    derived = {"pre_cc_last_frame_key": 0, "pristine_last_frame_key": 0}
    async with session_factory() as s:
        rows = (await s.execute(sa.text(
            "SELECT id, project_id, shot_id FROM shots"))).fetchall()
        for sid, pid, shot_id in rows:
            rel = f"projects/{pid}/shots/shot_{shot_id}"
            shot_dir = storage_root / rel
            if not shot_dir.is_dir():
                continue

            pre_cc = shot_dir / "last_frame_pre_cc.png"
            if pre_cc.is_file():
                n = (await s.execute(sa.text(
                    "UPDATE shots SET pre_cc_last_frame_key = :k WHERE id = :i "
                    "AND (pre_cc_last_frame_key IS NULL OR pre_cc_last_frame_key = '')"),
                    {"k": f"{rel}/last_frame_pre_cc.png", "i": sid})).rowcount
                derived["pre_cc_last_frame_key"] += n or 0

            # pristine：last_frame*.png 里排除固定名备份，取 mtime 最新
            cands = [p for p in shot_dir.glob("last_frame*.png")
                     if p.name != "last_frame_pre_cc.png"]
            if cands:
                newest = max(cands, key=lambda p: p.stat().st_mtime)
                n = (await s.execute(sa.text(
                    "UPDATE shots SET pristine_last_frame_key = :k WHERE id = :i "
                    "AND (pristine_last_frame_key IS NULL OR pristine_last_frame_key = '')"),
                    {"k": f"{rel}/{newest.name}", "i": sid})).rowcount
                derived["pristine_last_frame_key"] += n or 0
        await s.commit()
    return derived


async def backfill(storage_root: Path, session_factory) -> dict:
    """把 DB 里的路径值回填成 COS key，并推导两个新列的初值。

    需要停写窗口。开始前先跑一次幂等建列 —— 切换顺序里本步骤(第 5 步)早于
    部署新代码后的首次启动(第 6 步)，而两个 key 列正是在启动时由幂等
    ALTER TABLE 创建的；不先建列这一步会直接失败（Spec B §9.2）。

    注意这里用 ``_ensure_columns(conn)`` 而**不是** ``init_db()``：后者写死
    操作 app.db 模块级的 engine（指向真实 dev.db），在测试里会绕过传进来的
    session_factory 去动共享库 —— 那正是本计划明令禁止的事。``_ensure_columns``
    接收调用方的 conn，因此永远作用在正确的引擎上（Spec A 写它时就预留了
    这个用法，见其 docstring）。该例程幂等，重复执行无害。
    """
    from app.db import _ensure_columns

    async with session_factory() as s:
        conn = await s.connection()
        await _ensure_columns(conn)
        await s.commit()

    changed = skipped = 0
    unrecognized = []
    async with session_factory() as s:
        conn = await s.connection()
        for spec in FIELDS:
            if spec.optional_table and not await _table_exists(conn, spec.table):
                logger.info("skip_missing_table", extra={"table": spec.table})
                continue
            rows = (await s.execute(sa.text(
                f"SELECT {spec.pk}, {spec.column} FROM {spec.table} "
                f"WHERE {spec.column} IS NOT NULL AND {spec.column} <> ''"))).fetchall()
            for pk, raw in rows:
                if spec.kind == "scalar":
                    kind = classify(raw)
                    if kind == ALREADY_KEY:
                        skipped += 1
                        continue
                    if kind == UNRECOGNIZED:
                        unrecognized.append({"table": spec.table, "column": spec.column,
                                             "pk": str(pk), "raw": raw})
                        continue
                    new = to_key(raw)
                else:
                    fn = (rewrite_json_list if spec.kind == "json_list"
                          else rewrite_json_dict_of_lists)
                    new = fn(raw)
                    if new is None:
                        skipped += 1
                        continue
                await s.execute(sa.text(
                    f"UPDATE {spec.table} SET {spec.column} = :v WHERE {spec.pk} = :k"),
                    {"v": new, "k": pk})
                changed += 1
        await s.commit()

    derived = await _derive_key_columns(storage_root, session_factory)
    return {"phase": "backfill", "changed": changed, "skipped": skipped,
            "unrecognized": unrecognized, "derived": derived}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && uv run pytest tests/integration/test_cos_migration_flow.py -v
```
Expected: PASS（7 个用例；不需要凭证的那 5 个在无凭证 shell 里也应通过，另 2 个 SKIP）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/scripts/cos_migration/runner.py backend/tests/integration/test_cos_migration_flow.py
git commit -m "feat(migrate): --backfill 回填 key + 推导两个新列初值

先跑 init_db 建列，解掉 Spec B §9.2 指出的「回填早于建列」顺序陷阱。"
```

---

## Task 6: `--verify` —— 对悬空基线之外的缺失判失败

**Files:**
- Modify: `backend/app/scripts/cos_migration/runner.py`（追加 `verify()`）
- Test: `backend/tests/integration/test_cos_migration_flow.py`（追加 verify 用例）

**Interfaces:**
- Consumes: Task 3 的 `collect_db_refs`、scan 报告里的 `dangling`；Task 4 的 `key_prefix` 约定。
- Produces: `async def verify(session_factory, key_prefix: str = "", baseline: list[dict] | None = None) -> dict`，返回 `{"phase":"verify","checked":n,"present":n,"missing_expected":n,"missing_unexpected":[{...}],"ok":bool}`。`ok` 为 `False` 时 CLI 以退出码 1 结束。

- [ ] **Step 1: 写失败测试**

```python
@requires_cos
async def test_verify_passes_when_every_key_exists(db_session_factory, tmp_path, cos_prefix):
    pid, _ = await _seed_legacy_rows(db_session_factory, tmp_path)
    await upload(tmp_path, key_prefix=cos_prefix)
    await backfill(tmp_path, db_session_factory)

    report = await verify(db_session_factory, key_prefix=cos_prefix)
    assert report["ok"] is True
    assert report["missing_unexpected"] == []
    assert report["present"] == report["checked"]


@requires_cos
async def test_verify_tolerates_baseline_dangling_but_fails_on_new_gaps(
        db_session_factory, tmp_path, cos_prefix):
    """迁移前就已破损的引用进基线、不判失败；基线之外的缺失必须判失败，
    否则 --verify 这盏红绿灯就没有意义了。"""
    pid, shot_dir = await _seed_legacy_rows(db_session_factory, tmp_path)
    # last_frame 本地就没有 → 迁移前既有破损，进基线
    (shot_dir / "last_frame.png").unlink()

    scan_report = await scan(tmp_path, db_session_factory)
    await upload(tmp_path, key_prefix=cos_prefix)
    await backfill(tmp_path, db_session_factory)

    ok = await verify(db_session_factory, key_prefix=cos_prefix,
                      baseline=scan_report["dangling"])
    assert ok["ok"] is True
    assert ok["missing_expected"] == 1
    assert ok["missing_unexpected"] == []

    # 现在人为制造一个基线之外的缺口：删掉已上传的 output.mp4
    from app.services import object_store
    await object_store.delete(f"{cos_prefix}projects/{pid}/shots/shot_1/output.mp4")

    bad = await verify(db_session_factory, key_prefix=cos_prefix,
                       baseline=scan_report["dangling"])
    assert bad["ok"] is False
    assert [m["key"] for m in bad["missing_unexpected"]] == [
        f"projects/{pid}/shots/shot_1/output.mp4"]
```

import 行更新为：

```python
from app.scripts.cos_migration.runner import (
    backfill, collect_db_refs, scan, upload, verify,
)
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend
export COS_SECRET_ID=$(cat ../deploy/secrets/cos_secret_id)
export COS_SECRET_KEY=$(cat ../deploy/secrets/cos_secret_key)
uv run pytest tests/integration/test_cos_migration_flow.py -k verify -v
```
Expected: FAIL —— `ImportError: cannot import name 'verify'`。

- [ ] **Step 3: 实现 `verify()`**

```python
async def verify(session_factory, key_prefix: str = "",
                 baseline: Optional[list[dict]] = None) -> dict:
    """校验 DB 中每个 key 在 COS 真实存在。

    ``baseline`` 是 --scan 产出的悬空清单：那些引用的本地文件在迁移**之前**
    就已不存在（实测 312 条里有 200 条），迁移无从修复。把它们列为「预期缺失」
    单独计数，只对基线之外的缺口判失败——否则 --verify 会永远飘红，
    也就再没人拿它当红绿灯看了。
    """
    await cos_client.warm_credentials()
    expected_missing = {b["key"] for b in (baseline or []) if b.get("key")}

    refs = await collect_db_refs(session_factory)
    checked = present = missing_expected = 0
    missing_unexpected = []
    seen = set()
    for r in refs:
        if r.key is None or r.key in seen:
            continue
        seen.add(r.key)
        checked += 1
        if await object_store.exists(f"{key_prefix}{r.key}"):
            present += 1
        elif r.key in expected_missing:
            missing_expected += 1
        else:
            missing_unexpected.append(asdict(r))
    return {"phase": "verify", "checked": checked, "present": present,
            "missing_expected": missing_expected,
            "missing_unexpected": missing_unexpected,
            "ok": not missing_unexpected}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend
export COS_SECRET_ID=$(cat ../deploy/secrets/cos_secret_id)
export COS_SECRET_KEY=$(cat ../deploy/secrets/cos_secret_key)
uv run pytest tests/integration/test_cos_migration_flow.py -v
```
Expected: PASS（9 个用例）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/scripts/cos_migration/runner.py backend/tests/integration/test_cos_migration_flow.py
git commit -m "feat(migrate): --verify 校验 key 存在性，悬空基线之外的缺失判失败"
```

---

## Task 7: `migrate_to_cos.py` CLI

**Files:**
- Create: `backend/app/scripts/migrate_to_cos.py`
- Test: `backend/tests/integration/test_cos_migration_flow.py`（追加 CLI 用例）

**Interfaces:**
- Consumes: Task 3-6 的四个阶段函数。
- Produces: `python -m app.scripts.migrate_to_cos --scan|--upload|--backfill|--verify`，报告写入 `--report-dir`（默认 `migration_report/`）。`--verify` 失败时退出码 1。

- [ ] **Step 1: 写失败测试**

```python
async def test_cli_scan_writes_report_file(db_session_factory, tmp_path, monkeypatch):
    """CLI 必须把报告落盘——切换手册要人工核对它。"""
    from app.scripts.migrate_to_cos import main

    pid, _ = await _seed_legacy_rows(db_session_factory, tmp_path)
    report_dir = tmp_path / "reports"
    code = await main(["--scan", "--storage-root", str(tmp_path),
                       "--report-dir", str(report_dir)])
    assert code == 0
    written = json.loads((report_dir / "scan.json").read_text())
    assert written["phase"] == "scan"
    assert written["db"]["legacy_relative"] == 3
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && uv run pytest tests/integration/test_cos_migration_flow.py -k cli -v
```
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.scripts.migrate_to_cos'`。

- [ ] **Step 3: 实现 CLI**

创建 `backend/app/scripts/migrate_to_cos.py`：

```python
"""存量媒体迁移 CLI。

    uv run --project backend python -m app.scripts.migrate_to_cos --scan \
        --storage-root /app/storage

四个阶段可分别运行，全部幂等、可中断重跑。典型切换顺序见
docs/runbooks/2026-07-28-cos-cutover.md。
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from app.scripts.cos_migration.runner import backfill, scan, upload, verify, write_report


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="migrate_to_cos")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--scan", action="store_true", help="扫描本地与 DB，产出清单与悬空基线")
    g.add_argument("--upload", action="store_true", help="上传对象（可在线运行，不动 DB）")
    g.add_argument("--backfill", action="store_true", help="回填 DB 路径字段（需停写窗口）")
    g.add_argument("--verify", action="store_true", help="校验每个 key 在 COS 存在")
    p.add_argument("--storage-root", required=True, type=Path,
                   help="本地 storage 根目录（容器内通常是 /app/storage）")
    p.add_argument("--report-dir", type=Path, default=Path("migration_report"))
    p.add_argument("--key-prefix", default="",
                   help="仅测试用：把所有对象写到该前缀下，保持 dev bucket 干净")
    p.add_argument("--retry-failed", type=Path, default=None,
                   help="--upload 时按给定的上次报告只重试失败条目")
    return p


async def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _build_parser().parse_args(argv)

    # app/db.py 里 `AsyncSession` 就是 async_sessionmaker 实例（不是
    # sqlalchemy 的 AsyncSession 类），直接当 session_factory 用。绝不另建
    # 一个平行 engine —— 那会和应用用不同的连接池打同一个 sqlite 文件。
    from app.db import AsyncSession as session_factory
    sf = session_factory

    if args.scan:
        report = await scan(args.storage_root, sf)
    elif args.upload:
        only = None
        if args.retry_failed:
            prev = json.loads(args.retry_failed.read_text(encoding="utf-8"))
            only = [f["key"] for f in prev.get("failed", [])]
        report = await upload(args.storage_root, key_prefix=args.key_prefix, only=only)
    elif args.backfill:
        report = await backfill(args.storage_root, sf)
    else:
        baseline = None
        scan_path = args.report_dir / "scan.json"
        if scan_path.is_file():
            baseline = json.loads(scan_path.read_text(encoding="utf-8")).get("dangling")
        report = await verify(sf, key_prefix=args.key_prefix, baseline=baseline)

    path = write_report(report, args.report_dir, report["phase"])
    print(json.dumps(report, ensure_ascii=False, indent=2)[:4000])
    print(f"\n报告已写入 {path}")
    return 0 if report.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && uv run pytest tests/integration/test_cos_migration_flow.py -v
```
Expected: PASS（10 个用例）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/scripts/migrate_to_cos.py backend/tests/integration/test_cos_migration_flow.py
git commit -m "feat(migrate): migrate_to_cos CLI，四阶段可分别运行"
```

---

## Task 8: 堵住集成测试的孤儿泄漏源

**背景（实施者必读）：** dev bucket 此前积了 2895 个对象，其中 **2893 个是测试孤儿**（1581 个 DB 里根本不存在的 project id），已清理。根因不是 `cos_prefix`——那个 fixture 写在 `test/<uuid>/` 下且有 teardown；根因是 `tests/integration/conftest.py:195` 的 `seed_shot_with_source` 直接用 `shot_key(project_id, shot_id, ...)` 发布到**真实的 `projects/<uuid>/` 前缀**，而整个 conftest 里**没有任何 `delete_prefix` teardown**。不堵住它，Task 9 的巡检工具首日报告仍会全是测试垃圾、不可用。

**Files:**
- Modify: `backend/tests/integration/conftest.py`
- Test: `backend/tests/integration/test_cos_test_prefix_hygiene.py`（新建）

**Interfaces:**
- Produces: 自动生效的 `_cleanup_cos_project_prefixes` autouse fixture；模块级注册表 `register_test_project(project_id)`，供 `_make_project` 与 `make_project` 调用。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/integration/test_cos_test_prefix_hygiene.py`：

```python
"""守卫：集成测试不许在 dev bucket 的 projects/ 前缀下留孤儿对象。

历史教训：seed_shot_with_source 直接发布到真实 projects/<uuid>/ 且无
teardown，攒出 2893 个孤儿对象（1581 个 DB 里不存在的 project id），
把孤儿巡检报告淹没成噪音。
"""
import pytest

from tests.integration.conftest import _make_project, seed_shot_with_source
from tests.integration.conftest_cos import requires_cos


@requires_cos
async def test_seeded_shot_objects_are_cleaned_up(db_session_factory, cos_prefix):
    from app.services import object_store

    pid = await _make_project(db_session_factory)
    key = await seed_shot_with_source(db_session_factory, pid, 1, frames=12)
    assert await object_store.exists(key)

    # 模拟 teardown：本用例结束后 autouse fixture 会删掉 projects/<pid>/，
    # 这里显式调一次同样的清理并断言它确实生效。
    from tests.integration.conftest import cleanup_test_project_prefixes
    await cleanup_test_project_prefixes()
    assert not await object_store.exists(key)
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend
export COS_SECRET_ID=$(cat ../deploy/secrets/cos_secret_id)
export COS_SECRET_KEY=$(cat ../deploy/secrets/cos_secret_key)
uv run pytest tests/integration/test_cos_test_prefix_hygiene.py -v
```
Expected: FAIL —— `ImportError: cannot import name 'cleanup_test_project_prefixes'`。

- [ ] **Step 3: 在 conftest.py 加注册表与清理**

在 `backend/tests/integration/conftest.py` 靠近顶部处加入：

```python
# 集成测试创建的 project id 注册表。seed_shot_with_source 等 helper 会把
# 素材发布到真实的 projects/<id>/ 前缀（不是 cos_prefix 的 test/ 前缀），
# 不清理就会在 dev bucket 里永久堆积——历史上攒出过 2893 个孤儿对象，
# 直接把孤儿巡检报告淹没。
_test_project_ids: list[str] = []


def register_test_project(project_id: str) -> str:
    _test_project_ids.append(project_id)
    return project_id


async def cleanup_test_project_prefixes() -> int:
    """删掉本次测试创建的所有 projects/<id>/ 前缀。返回删除的对象数。"""
    if not _test_project_ids:
        return 0
    from tests.integration.conftest_cos import _cos_configured
    if not _cos_configured():
        _test_project_ids.clear()
        return 0
    from app.services import cos_client, object_store
    await cos_client.warm_credentials()
    n = 0
    for pid in _test_project_ids:
        n += await object_store.delete_prefix(f"projects/{pid}/")
    _test_project_ids.clear()
    return n


@pytest.fixture(autouse=True)
async def _cleanup_cos_project_prefixes():
    _test_project_ids.clear()
    yield
    await cleanup_test_project_prefixes()
```

`_make_project`（约 132 行）末尾把 `return p.id` 改成：

```python
        return register_test_project(p.id)
```

`make_project` fixture（约 245 行，走 HTTP API 创建，返回的是整个 project dict）里把 `return r.json()` 改成：

```python
        data = r.json()
        register_test_project(data["id"])
        return data
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend
export COS_SECRET_ID=$(cat ../deploy/secrets/cos_secret_id)
export COS_SECRET_KEY=$(cat ../deploy/secrets/cos_secret_key)
uv run pytest tests/integration/test_cos_test_prefix_hygiene.py tests/unit/test_cos_gating_hygiene.py -v
```
Expected: PASS。

- [ ] **Step 5: 跑一遍完整集成测试并确认 bucket 没有新增孤儿**

```bash
cd backend
export COS_SECRET_ID=$(cat ../deploy/secrets/cos_secret_id)
export COS_SECRET_KEY=$(cat ../deploy/secrets/cos_secret_key)
uv run pytest tests/integration -v 2>&1 | tail -20
```
Expected: 与本任务前的通过/跳过数一致（本任务不改被测行为，只加 teardown）。随后用 Task 9 的巡检工具核对 bucket 对象数没有明显增长。

- [ ] **Step 6: Commit**

```bash
git add backend/tests/integration/conftest.py backend/tests/integration/test_cos_test_prefix_hygiene.py
git commit -m "test(cos): 集成测试 teardown 清理 projects/ 前缀，堵住孤儿泄漏源

seed_shot_with_source 发布到真实 projects/<uuid>/ 却无 teardown，
此前在 dev bucket 攒出 2893 个孤儿对象。"
```

---

## Task 9: 孤儿对象巡检（仅 dry-run）

**Files:**
- Create: `backend/app/scripts/cos_orphan_report.py`
- Test: `backend/tests/integration/test_cos_orphan_report.py`

**Interfaces:**
- Consumes: Task 3 的 `collect_db_refs`；`object_store.list_prefix`。
- Produces: `async def find_orphans(session_factory, prefixes=("projects/","analyses/"), older_than_days=0, key_prefix="") -> dict`，返回 `{"orphans":[{"key":...,"bytes":n}],"count":n,"bytes":n,"scanned":n}`；`async def main(argv=None) -> int`。

**硬约束：本工具绝不删除任何对象。** 它是整套设计里唯一具备不可逆破坏风险的组件，因此本次只做 dry-run（Spec B §5）。必须覆盖 `projects/` 与 `analyses/` **两个**前缀。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/integration/test_cos_orphan_report.py`：

```python
"""孤儿巡检：报告准确性 + 绝不删除。"""
import pytest
import sqlalchemy as sa

from app.scripts.cos_orphan_report import find_orphans
from tests.integration.conftest_cos import requires_cos


@requires_cos
async def test_reports_known_orphans_and_spares_referenced_objects(
        db_session_factory, tmp_path, cos_prefix):
    from app.services import object_store

    pid = "22222222-2222-2222-2222-222222222222"
    referenced = f"projects/{pid}/shots/shot_1/output.mp4"
    orphan_a = f"projects/{pid}/shots/shot_1/leftover_a.mp4"
    orphan_b = f"analyses/{pid}/samples/1/leftover_b.mp4"

    for k in (referenced, orphan_a, orphan_b):
        p = tmp_path / "blob.bin"
        p.write_bytes(b"x" * 16)
        await object_store.put(f"{cos_prefix}{k}", p)

    async with db_session_factory() as s:
        await s.execute(sa.text(
            "INSERT INTO projects (id,title,theme_text,creator_name,status,aspect_ratio) "
            "VALUES (:i,'t','t','a','draft','9:16')"), {"i": pid})
        await s.execute(sa.text(
            "INSERT INTO shots (project_id,shot_id,text,shot_type,visual_description,"
            "status,video_path) VALUES (:p,1,'t','Wide','v','completed',:v)"),
            {"p": pid, "v": referenced})
        await s.commit()

    report = await find_orphans(db_session_factory, key_prefix=cos_prefix)

    assert set(report["orphans_keys"]) == {orphan_a, orphan_b}
    assert report["count"] == 2
    assert referenced not in report["orphans_keys"]
    # analyses/ 前缀必须被覆盖——Spec B §2.2 漏掉 reference_samples 就是
    # 因为只想着 projects/
    assert any(k.startswith("analyses/") for k in report["orphans_keys"])


@requires_cos
async def test_never_deletes_anything(db_session_factory, tmp_path, cos_prefix, monkeypatch):
    """本工具是整套设计里唯一有不可逆破坏风险的组件，本次只做 dry-run。
    这里把所有删除入口换成会炸的替身——不是在 mock 被测逻辑，而是断言
    「删除从未被调用」这件事本身。"""
    from app.services import object_store

    p = tmp_path / "blob.bin"
    p.write_bytes(b"x" * 16)
    await object_store.put(f"{cos_prefix}projects/zzz/orphan.mp4", p)

    def _boom(*a, **k):
        raise AssertionError("巡检工具绝不允许删除任何对象")

    monkeypatch.setattr(object_store, "delete", _boom)
    monkeypatch.setattr(object_store, "delete_prefix", _boom)

    report = await find_orphans(db_session_factory, key_prefix=cos_prefix)
    assert report["count"] >= 1
    # 对象仍在
    assert await object_store.exists(f"{cos_prefix}projects/zzz/orphan.mp4")
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend
export COS_SECRET_ID=$(cat ../deploy/secrets/cos_secret_id)
export COS_SECRET_KEY=$(cat ../deploy/secrets/cos_secret_key)
uv run pytest tests/integration/test_cos_orphan_report.py -v
```
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.scripts.cos_orphan_report'`。

- [ ] **Step 3: 实现巡检工具**

创建 `backend/app/scripts/cos_orphan_report.py`：

```python
"""孤儿对象巡检 —— **只读，绝不删除**。

Spec A 的一致性约定（宁可留孤儿，绝不留悬空引用）会持续产生无人引用的
对象。本工具比对 COS 实际对象与 DB 中全部 key 引用，输出孤儿清单与总体积。

为什么只做 dry-run（Spec B §5）：它是整套设计里唯一具备不可逆破坏风险的
组件。先用报告观察真实孤儿量与增长速度，再决定是否值得开启自动清理；
若孤儿量微不足道，自动清理就不必做。开启自动删除的前置条件：bucket 多版本
控制已开启（当前**未开启**）、dry-run 报告经过至少数周观察、删除本身也先
跑 dry-run。

    uv run --project backend python -m app.scripts.cos_orphan_report --older-than-days 7
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from app.scripts.cos_migration.runner import collect_db_refs
from app.services import cos_client, object_store

logger = logging.getLogger(__name__)

DEFAULT_PREFIXES = ("projects/", "analyses/")


def _parse_last_modified(value: str):
    """COS 返回的 ISO8601，形如 2026-07-27T09:05:11.000Z。"""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


async def find_orphans(session_factory, prefixes=DEFAULT_PREFIXES,
                       older_than_days: int = 0, key_prefix: str = "") -> dict:
    """列出 COS 上无人引用的对象。**不删除任何东西。**"""
    await cos_client.warm_credentials()

    refs = await collect_db_refs(session_factory)
    referenced = {r.key for r in refs if r.key}

    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    orphans, scanned = [], 0
    for pre in prefixes:
        client = cos_client.get_client()
        marker = ""
        while True:
            r = await asyncio.to_thread(
                client.list_objects, Bucket=cos_client.bucket(),
                Prefix=f"{key_prefix}{pre}", Marker=marker)
            for obj in r.get("Contents", []):
                scanned += 1
                full = obj["Key"]
                key = full[len(key_prefix):] if key_prefix else full
                if key in referenced:
                    continue
                if older_than_days:
                    lm = _parse_last_modified(obj.get("LastModified", ""))
                    if lm is not None and lm > cutoff:
                        continue
                orphans.append({"key": key, "bytes": int(obj["Size"]),
                                "last_modified": obj.get("LastModified")})
            if r.get("IsTruncated") != "true":
                break
            marker = r.get("NextMarker") or (r["Contents"][-1]["Key"] if r.get("Contents") else "")
            if not marker:
                break

    return {
        "phase": "orphan_report",
        "dry_run": True,
        "scanned": scanned,
        "count": len(orphans),
        "bytes": sum(o["bytes"] for o in orphans),
        "orphans_keys": [o["key"] for o in orphans],
        "orphans": sorted(orphans, key=lambda o: -o["bytes"])[:200],
    }


async def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="cos_orphan_report")
    p.add_argument("--older-than-days", type=int, default=7,
                   help="只报告早于该天数的对象，避开正在写入的新对象")
    p.add_argument("--key-prefix", default="", help="仅测试用")
    args = p.parse_args(argv)

    from app.db import AsyncSession as session_factory
    report = await find_orphans(session_factory,
                                older_than_days=args.older_than_days,
                                key_prefix=args.key_prefix)
    print(json.dumps(report, ensure_ascii=False, indent=2)[:8000])
    print(f"\n孤儿对象 {report['count']} 个，合计 {report['bytes'] / 1e6:.1f} MB")
    print("本工具为 dry-run，未删除任何对象。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend
export COS_SECRET_ID=$(cat ../deploy/secrets/cos_secret_id)
export COS_SECRET_KEY=$(cat ../deploy/secrets/cos_secret_key)
uv run pytest tests/integration/test_cos_orphan_report.py tests/unit/test_cos_gating_hygiene.py -v
```
Expected: PASS。

- [ ] **Step 5: 对真实 dev bucket 跑一次，确认报告可用**

```bash
cd backend
export COS_SECRET_ID=$(cat ../deploy/secrets/cos_secret_id)
export COS_SECRET_KEY=$(cat ../deploy/secrets/cos_secret_key)
uv run python -m app.scripts.cos_orphan_report --older-than-days 0
```
Expected: 孤儿数为个位数（清理后 bucket 只剩 2 个对象且都被引用）。若报出成百上千，说明 Task 8 的 teardown 没生效，回头查。

- [ ] **Step 6: Commit**

```bash
git add backend/app/scripts/cos_orphan_report.py backend/tests/integration/test_cos_orphan_report.py
git commit -m "feat(cos): 孤儿对象巡检工具（仅 dry-run，覆盖 projects/ 与 analyses/）"
```

---

## Task 10: 生产切换手册

**Files:**
- Create: `docs/runbooks/2026-07-28-cos-cutover.md`

- [ ] **Step 1: 写手册**

创建 `docs/runbooks/2026-07-28-cos-cutover.md`，内容必须包含：

```markdown
# COS 存量迁移与生产切换手册

## 执行前提

- 本仓 Spec A（存储层 COS 化）已部署
- **bucket 多版本控制已开启**（当前 dev bucket 未开启，切换前必须先开）
- 已确认 bucket 与 CVM 同地域（跨地域会让每次 ffmpeg 取素材按外网下行计费）

## 七步

1. **备份 DB**：`cp` sqlite 文件到带时间戳的副本；确认 bucket 版本控制已开启
2. **在线 `--upload`**（不停服，最耗时的一步在此消化）
   ```bash
   uv run --project backend python -m app.scripts.migrate_to_cos \
       --upload --storage-root /app/storage
   ```
3. **停止服务**：`podman compose -f deploy/docker-compose.dev.yml down`
4. **再次 `--upload` 补增量**（停服前最后写入的文件）
5. **`--scan` 后 `--backfill`**（先 scan 产出悬空基线供第 7 步使用）
   ```bash
   uv run --project backend python -m app.scripts.migrate_to_cos --scan --storage-root /app/storage
   uv run --project backend python -m app.scripts.migrate_to_cos --backfill --storage-root /app/storage
   ```
   > **建列顺序陷阱（Spec B §9.2）**：两个 key 列由 `app/db.py` 的幂等
   > `ALTER TABLE` 在应用启动时创建，而本步骤早于第 6 步部署后的首次启动。
   > `--backfill` 已在内部先调 `init_db()` 建列，**不要**跳过或替换该步骤。
6. **部署新代码**
7. **启动 + `--verify`**：退出码 0 即全绿

## 验收

- `--verify` 退出码 0（`missing_unexpected` 为空）
- 人工抽查若干历史项目：视频可播放、尾帧可显示、成片可下载
- 抽查一个做过 CC 的历史分镜，确认还原正常（验证 `pristine_last_frame_key` /
  `pre_cc_last_frame_key` 填对了）

## 回滚

恢复 DB 备份 + 回滚代码。**COS 上的对象不删除**（保留对下次重试有用，且只占存储费）。

## 迁移后

- 本地 `storage/` 目录**先保留一到两周**作为回滚保险。清理前必须确认两个 key 列
  已正确填充——本地目录是这些信息的唯一来源，**清掉就永远补不回来**。
- 一周内配置生命周期规则（不做自动 GC 时，这是控制存储成本的唯一手段）
- 设置流量告警（免费额度不含流量，流量是本项目主要成本项）

## 已知的预期缺失

实测共享 dev DB 的 312 条媒体引用里有 **200 条**的本地文件在迁移**之前**就已不存在
（55 个项目目录整体消失，含 19 个 `shot_review`、2 个 `exported`）。这是既有破损，
迁移无法修复。`--scan` 会把它们固化成悬空基线，`--verify` 将其计入
`missing_expected` 而不判失败。**基线之外的任何缺失都必须当作真故障处理。**
```

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/2026-07-28-cos-cutover.md
git commit -m "docs(runbook): COS 切换手册（七步 + 建列顺序陷阱 + 悬空基线说明）"
```

---

## 不在本计划范围内

- **Spec B §3「生产切换」的实际执行**（阶段 7）：这是一次带停机窗口的运维动作，需要人工在真实环境按 Task 10 的手册执行，不由本计划的代码任务代劳。
- **Spec B §6 的八项运维待办**：全部是腾讯云 COS 控制台 / CAM 控制台的配置项，人工执行。其中第 1 项（开启多版本控制）**当前未完成**，是切换前的硬前提。
- **孤儿对象的自动删除**：Spec B §5 明确本次只做 dry-run。
