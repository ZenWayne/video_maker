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
    """判断一个 DB 路径值的形态。空值/认不出的返回 UNRECOGNIZED。

    关键：只有当转换后确实得到合法 key（以 KEY_PREFIXES 开头）时，
    才返回 LEGACY_REL/LEGACY_ABS，否则返回 UNRECOGNIZED。
    这保证了 to_key 的幂等性：classify 返回的形态总是能成功转换。
    """
    if not value:
        return UNRECOGNIZED

    # 已经是 key
    if value.startswith(KEY_PREFIXES):
        return ALREADY_KEY

    # 检查 LEGACY_REL：转换后必须是非空且以合法前缀开头
    if value.startswith(LEGACY_REL_PREFIX):
        candidate = value[len(LEGACY_REL_PREFIX):]
        if candidate and candidate.startswith(KEY_PREFIXES):
            return LEGACY_REL

    # 检查 LEGACY_ABS：转换后必须是非空且以合法前缀开头
    if value.startswith("/") and _ABS_MARKER in value:
        candidate = value.split(_ABS_MARKER, 1)[1]
        if candidate and candidate.startswith(KEY_PREFIXES):
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
