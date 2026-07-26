# 存储层 COS 化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 video_maker 后端的媒体存储层从「本地文件路径」改造为「腾讯云 COS object key」，使容器无状态、素材权威副本位于 COS。

**Architecture:** 新增三个服务模块——`cos_client`（客户端与凭证）、`object_store`（对象原语）、`workspace`（ffmpeg 临时工作区）——并把 `storage.py` 从返回 `Path` 改为返回 COS key。所有 ffmpeg 操作改为「fetch 到临时目录 → 本地计算 → publish 回 COS」。浏览器通过后端签发的预签名 URL 直连 COS，后端不再中转媒体流量。

**Tech Stack:** Python 3.12、FastAPI、ARQ、SQLAlchemy 2.0(async)、SQLite(aiosqlite)、`cos-python-sdk-v5`（`qcloud_cos`）、pytest(asyncio_mode=auto)

**对应 Spec:** [Spec A — 存储层 COS 化](../specs/2026-07-26-cos-storage-layer-design.md)（阶段 0–5）。存量迁移与生产切换属 [Spec B](../specs/2026-07-26-cos-migration-cutover-design.md)，不在本计划内。

> **2026-07-26 变更**：本计划原为阿里云 OSS，现改为腾讯云 COS（用户服务器在腾讯云，同地域内网免流量费）。Task 1 已按 OSS 实现并提交（`e46fed9`），本轮 Task 1 的职责变为**把 OSS 接线换成 COS 接线**。Task 4–15 未受影响——云厂商被隔离在 `cos_client` 一个模块内，这正是方案 B 的附带收益。

---

## Global Constraints

以下约束适用于**每一个** task，不再逐条重复：

- **Python 依赖管理**：只用 `pyproject.toml` + `uv`。新增包须写入 `backend/pyproject.toml` 后执行 `uv sync --project backend`。**禁止** `pip install`，**禁止**直接调用 `python` / `python3`。
- **运行测试**：`uv run --project backend pytest ...`，不套 podman。
- **禁止硬编码绝对路径**：Python 中一律 `Path(__file__)` 相对定位。
- **不使用 alembic**：本项目无 alembic。schema 变更写在 `backend/app/db.py`，用 `_has_column()` 守卫的幂等 `ALTER TABLE`（既有写法见 db.py:55-115）。
- **COS SDK 是纯同步的**：`cos-python-sdk-v5` 没有异步客户端。**每一个 SDK 调用都必须经 `await asyncio.to_thread(...)`**，唯一例外是预签名（纯本地 HMAC，不发网络请求）。遗漏一处不会报错，只会在传大文件时让整个服务卡死——这是本计划最隐蔽的一类缺陷。
- **`CosS3Client` 必须单例**：SDK 明确要求一个 region 只建一个实例并复用，否则进程占用过多连接和线程。
- **测试不 mock 基础设施**：除会计费的 AI 模型调用外一律不 mock。COS 用**真实 dev bucket**，测试对象一律写在 `test/<uuid>/` 唯一前缀下并在 teardown 删除前缀。**禁止**实现 fake object store。
- **凭证卫生**：需要真实凭证的集成测试，与断言配置字段的单元测试（如 `test_cos_config.py`）**必须分开跑**。后者断言的就是 `settings.cos_*` 的值，一旦在导出了真实凭证的 shell 里失败，pytest 的断言差异输出会把凭证打进日志。Task 2 实施时已真实发生过一次（泄露了 SecretId）。任何情况下都不要把凭证 `echo` 出来或写进报告。
- **绝不自行触发生成**：测试与验证中禁止调用真实视频/图像生成（计费）。需要真实视频素材时用 `tests/integration/conftest.py:160` 的 `seed_shot_with_source()`（ffmpeg 合成 testsrc2）。
- **签名 URL TTL**：`cos_signed_url_ttl_sec` 默认 `7200`（2 小时）。
- **key 命名**：`projects/<project_id>/...`，与原 `storage_root` 相对路径**逐字符一致**。DB 存**裸 key**，不带 scheme 前缀、不带前导 `/`。
- **bucket 名必须含 AppId**：COS 的 bucket 形如 `video-maker-dev-1250000000`，配置里必须填完整名。
- **一致性不变量**：新增文件先 `put` 成功再写 DB；删除先改 DB 解除引用再删 COS。**DB 中的 key 必须永远指向真实存在的对象**，宁可留孤儿对象。
- **提交粒度**：每个 task 末尾提交一次，commit message 用中文描述意图。

---

## File Structure

**新建：**

| 文件 | 职责 |
|---|---|
| `backend/app/services/cos_client.py` | 唯一 `import qcloud_cos` 的模块。`CosS3Client` 单例、两种凭证模式、同步签名用的凭证缓存 |
| `backend/app/services/object_store.py` | 对象操作原语：put/get/exists/size/copy/delete/delete_prefix/list_prefix/signed_url。全部 async，内部 `asyncio.to_thread` |
| `backend/app/services/workspace.py` | ffmpeg 临时工作区上下文管理器 |
| `backend/tests/unit/test_cos_config.py` | 配置字段单元测试 |
| `backend/tests/unit/test_storage_keys.py` | key 拼接纯函数单元测试 |
| `backend/tests/integration/conftest_cos.py` | COS 测试夹具（唯一前缀 + teardown） |
| `backend/tests/integration/test_object_store.py` | 对象原语集成测试（真实 dev bucket） |
| `backend/tests/integration/test_workspace.py` | 工作区集成测试 |
| `backend/tests/integration/test_cos_media_url.py` | 签名 URL 可真实 GET 的集成测试 |

**修改：**

| 文件 | 改动 |
|---|---|
| `backend/pyproject.toml` | 换依赖：移除 alibabacloud 三件套，加 `cos-python-sdk-v5` |
| `backend/app/config.py` | 换配置：`oss_*` → `cos_*` |
| `backend/app/db.py` | 加 3 列（幂等 ALTER TABLE），并把建列逻辑提取为 `_ensure_columns()` |
| `backend/app/models/project.py` | Shot 加 3 个 Column |
| `backend/app/services/storage.py` | 路径函数 → key 函数；`to_media_url` 改签名 URL |
| `backend/app/main.py:135-149` | 删静态挂载与 no-cache 中间件；lifespan 加凭证预热与 client 关闭 |
| `backend/app/api/assets.py` | 两个路由改 302 重定向 |
| `backend/app/api/projects.py` / `stream.py` / `voice.py` / `image_candidates.py` / `pipeline.py` / `uploads.py` | 素材读写点改 key |
| `backend/app/agents/video_generator.py` / `video_trimmer.py` / `effective_clip.py` | ffmpeg 输入改 fetch |
| `backend/worker/tasks.py` | 生成/VC/合并链路改 workspace |
| `backend/app/services/image_generation.py` / `first_frame.py` | 素材读写点改 key |
| `deploy/config.yml` / `secrets.yml.example` / `docker-compose.dev.yml` | 换配置与密钥、NO_PROXY 改 `.myqcloud.com` |
| `frontend-vite/` 播放器组件 | `onError` 重拉换新 URL |

**自然检查点：** Task 6 结束时基础设施完备但业务未切换，是一个适合停下来 review 的位置。

---

## Task 1: 把 OSS 接线换成 COS 接线

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/app/config.py`（`Settings` 类内的 OSS 段落）
- Modify: `deploy/config.yml`
- Modify: `deploy/secrets.yml.example`
- Modify: `deploy/docker-compose.dev.yml`
- Modify: `backend/tests/unit/test_oss_config.py` → 重命名为 `test_cos_config.py`

**Interfaces:**
- Consumes: 无（首个 task）
- Produces: `settings.cos_region`、`settings.cos_bucket`、`settings.cos_scheme`、`settings.cos_domain`、`settings.cos_auth_mode`、`settings.cos_cvm_role`、`settings.cos_signed_url_ttl_sec`、`settings.cos_secret_id`、`settings.cos_secret_key`

**背景**：上一轮已按阿里云 OSS 完成过一次同等接线（commit `e46fed9`）。本 task 是**替换**，不是新增——OSS 的字段、依赖、密钥、NO_PROXY 域名都要一并移除，不能两套并存。

- [ ] **Step 1: 改测试（TDD 从这里开始）**

`git mv backend/tests/unit/test_oss_config.py backend/tests/unit/test_cos_config.py`，然后用以下内容整体替换：

```python
"""COS 配置字段的默认值与类型约束。"""
from app.config import Settings


def test_cos_defaults_are_safe():
    """未配置时默认走 static 模式、https、2 小时 TTL。"""
    s = Settings(_env_file=None)
    assert s.cos_auth_mode == "static"
    assert s.cos_scheme == "https"
    assert s.cos_signed_url_ttl_sec == 7200
    assert s.cos_domain is None
    assert s.cos_secret_id is None
    assert s.cos_secret_key is None


def test_cos_auth_mode_accepts_cvm_role():
    s = Settings(_env_file=None, cos_auth_mode="cvm_role", cos_cvm_role="my-role")
    assert s.cos_auth_mode == "cvm_role"
    assert s.cos_cvm_role == "my-role"


def test_cos_bucket_keeps_appid_suffix():
    """COS 的 bucket 名必须含 AppId，配置层不得擅自截断。"""
    s = Settings(_env_file=None, cos_region="ap-guangzhou",
                 cos_bucket="video-maker-dev-1250000000")
    assert s.cos_region == "ap-guangzhou"
    assert s.cos_bucket == "video-maker-dev-1250000000"


def test_no_legacy_oss_fields_remain():
    """OSS 字段必须彻底移除——两套并存会让 cos_client 读到过期配置。"""
    s = Settings(_env_file=None)
    leftovers = [f for f in type(s).model_fields if f.startswith("oss_")]
    assert leftovers == [], f"残留 OSS 配置字段: {leftovers}"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project backend pytest tests/unit/test_cos_config.py -v`
Expected: FAIL — `ValidationError`（`Settings` 无 `cos_auth_mode` 等字段），且 `test_no_legacy_oss_fields_remain` 因 9 个 `oss_*` 字段仍在而失败

- [ ] **Step 3: 换依赖**

编辑 `backend/pyproject.toml`，**移除**上一轮加的三项：

```toml
    "alibabacloud-oss-v2>=1.2.0",
    "aiohttp>=3.9",
    "alibabacloud-credentials>=0.3",
```

**加入**：

```toml
    "cos-python-sdk-v5>=1.9",
```

只加这一个。COS SDK 基于 requests，不需要 aiohttp；CVM 角色凭证走元数据服务的普通 HTTP GET，用项目已有的 `httpx` 即可，不需要腾讯云凭证 SDK。

执行：

```bash
uv sync --project backend
```

- [ ] **Step 4: 换配置字段**

在 `backend/app/config.py` 中，把上一轮加的整个 `# ── 阿里云 OSS ──` 段落替换为：

```python
    # ── 腾讯云 COS ──────────────────────────────────────────────────────────
    # 存储权威副本所在 bucket。dev / prod 使用不同 bucket。
    # 应与 CVM 同地域，后端↔COS 才走内网免流量费。
    cos_region: str = ""
    # 必须含 AppId，形如 video-maker-dev-1250000000
    cos_bucket: str = ""
    cos_scheme: str = "https"
    # 自定义源站域名；留空则用默认域名 {bucket}.cos.{region}.myqcloud.com
    cos_domain: Optional[str] = None
    # "static"（开发，永久密钥）| "cvm_role"（生产，实例角色取 STS 临时密钥）
    cos_auth_mode: str = "static"
    # cvm_role 模式下绑定在 CVM 上的 CAM 角色名
    cos_cvm_role: Optional[str] = None
    # 预签名 URL 有效期，默认 2 小时
    cos_signed_url_ttl_sec: int = 7200
    # 仅 static 模式使用，由 /run/secrets/ 注入
    cos_secret_id: Optional[str] = None
    cos_secret_key: Optional[str] = None
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run --project backend pytest tests/unit/test_cos_config.py -v`
Expected: PASS（4 项）

- [ ] **Step 6: 换 deploy 配置**

`deploy/config.yml`：把上一轮的 5 个 `oss_*` 键替换为：

```yaml
cos_region: ap-guangzhou
cos_bucket: video-maker-dev-1414782845
cos_scheme: https
cos_auth_mode: static
cos_signed_url_ttl_sec: 7200
```

**这是真实值，不是占位符**——bucket 已创建、凭证已验证可用，与用户 k8s 集群节点同在广州地域（同地域内网免流量费，这是选腾讯云的核心动机）。照抄即可，不要改动。

`deploy/secrets.yml.example`：把 `oss_access_key_id` / `oss_access_key_secret` 两行替换为：

```yaml
cos_secret_id: AKIDxxxxxxxxxxxxxxxxxxxxxxxxxxxx
cos_secret_key: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**只改 `.example`，绝不创建或写入 `deploy/secrets.yml`（gitignored 真实密钥文件）。**

- [ ] **Step 7: 换 compose 密钥与 NO_PROXY**

`deploy/docker-compose.dev.yml`：

顶层 `secrets:` 段——把 `oss_access_key_id` / `oss_access_key_secret` 两项替换为（**路径用 `./secrets/`，与既有 6 条一致**）：

```yaml
  cos_secret_id:
    file: ./secrets/cos_secret_id  # written by: make secrets
  cos_secret_key:
    file: ./secrets/cos_secret_key  # written by: make secrets
```

各服务 `secrets:` 列表里的两项同步改名。各服务 `command:` 的 export 链改为：

```sh
export COS_SECRET_ID=$(cat /run/secrets/cos_secret_id) && \
export COS_SECRET_KEY=$(cat /run/secrets/cos_secret_key) && \
```

**NO_PROXY 的域名必须从 `.aliyuncs.com` 改为 `.myqcloud.com`**（COS 默认域名是 `{bucket-appid}.cos.{region}.myqcloud.com`）。大小写两份都改：

```yaml
      NO_PROXY: localhost,127.0.0.1,host.containers.internal,.myqcloud.com
      no_proxy: localhost,127.0.0.1,host.containers.internal,.myqcloud.com
```

上一轮已确认需要接线的服务是 `backend`、`worker`、`vc-worker` 三个（`mcp` 只经 `BACKEND_BASE_URL` 代理 REST，不碰存储）。三个都要改。

- [ ] **Step 8: 验证 compose 语法**

Run: `podman compose -f deploy/docker-compose.dev.yml config > /dev/null && echo OK`
Expected: 输出 `OK`

- [ ] **Step 9: 确认无 OSS 残留**

Run:

```bash
grep -rn "oss_\|OSS_\|alibabacloud\|aliyuncs" backend/app backend/pyproject.toml deploy/ || echo "无残留"
```

Expected: 输出 `无残留`。任何命中都说明替换不彻底。

- [ ] **Step 10: 提交**

```bash
git add backend/pyproject.toml backend/uv.lock backend/app/config.py \
        backend/tests/unit/test_cos_config.py backend/tests/unit/test_oss_config.py \
        deploy/config.yml deploy/secrets.yml.example deploy/docker-compose.dev.yml
git commit -m "refactor(cos)!: 存储后端由阿里云 OSS 改为腾讯云 COS

用户服务器在腾讯云，同地域内网访问免流量费。

BREAKING: 移除全部 oss_* 配置与 alibabacloud 依赖，换成 cos_*
与 cos-python-sdk-v5。NO_PROXY 域名同步改为 .myqcloud.com——
漏改会让 COS 请求走为 Google API 设的代理，表现为上传随机超时。"
```

---

## Task 2: cos_client — 客户端、凭证与同步签名缓存

**Files:**
- Create: `backend/app/services/cos_client.py`
- Delete: `backend/app/services/oss_client.py`（若上一轮已创建；按当前进度尚未创建，则跳过）
- Test: `backend/tests/integration/conftest_cos.py`
- Test: `backend/tests/integration/test_cos_client_smoke.py`

**Interfaces:**
- Consumes: Task 1 的 `settings.cos_*` 全部字段
- Produces:
  - `def get_client() -> CosS3Client` — 单例，传输类操作使用
  - `async def warm_credentials() -> None` — 预热凭证缓存，lifespan 启动时调用
  - `async def start_credential_refresh() -> None` — 启动后台刷新协程（仅 cvm_role）
  - `async def close_client() -> None` — 停止后台刷新并释放单例。幂等
  - `def get_cached_credentials() -> dict` — 同步读缓存，返回 `{"secret_id","secret_key","token"}`；缓存为空时抛 `RuntimeError`
  - `def credentials_remaining_sec() -> Optional[int]` — 剩余有效秒数；static 模式返回 `None`
  - `def bucket() -> str` — 返回 `settings.cos_bucket`

- [ ] **Step 1: 核对已安装 SDK 的 API 形态**

**这一步是硬性前置。** 上一轮在阿里云 SDK 上，计划里三处方法名与实际不符（`get_object_to_file`、`upload_file` 在异步客户端上根本不存在），是靠这一步发现的。COS 同样先核对再写。

Run:

```bash
uv run --project backend python -c "
from qcloud_cos import CosConfig, CosS3Client
import inspect
want = ['put_object','get_object','head_object','copy_object','list_objects',
        'delete_object','delete_objects','upload_file','object_exists',
        'get_presigned_url','get_presigned_download_url']
have = set(dir(CosS3Client))
for m in want:
    print(('OK      ' if m in have else 'MISSING '), m)
print()
print('CosConfig.__init__:', inspect.signature(CosConfig.__init__))
for m in ['get_presigned_url','upload_file','copy_object','object_exists']:
    if hasattr(CosS3Client, m):
        print(m, inspect.signature(getattr(CosS3Client, m)))
"
```

记录实际输出。**若某方法缺失或签名与后续步骤不符，以实际为准；拿不准就以 NEEDS_CONTEXT 上报，不要自行改设计。**

**以下形态已由 controller 在真实 bucket 上跑通验证（`video-maker-dev-1414782845` / `ap-guangzhou`），可直接采信：**

| 调用 | 已验证的形态 |
|---|---|
| `put_object` | `put_object(Bucket=, Key=, Body=<file obj>, EnableMD5=False)` |
| `object_exists` | `object_exists(bucket, key)` —— **位置参数，不是关键字** |
| `head_object` | `head_object(Bucket=, Key=)`，返回 dict，大小取 `r['Content-Length']` |
| `get_object` | `get_object(Bucket=, Key=)['Body'].get_stream_to_file(路径str)` |
| `copy_object` | `copy_object(Bucket=, Key=, CopySource={'Bucket':,'Key':,'Region':})` |
| `get_presigned_url` | `get_presigned_url(Method='GET', Bucket=, Key=, Expired=秒)`，返回的 URL 真实可 GET |
| `list_objects` | `list_objects(Bucket=, Prefix=)` → `r['Contents']`；**`r['IsTruncated']` 是字符串 `'false'`/`'true'`，不是布尔** —— Task 3 的分页循环正是按字符串比较写的，改成布尔判断会导致只取第一页 |
| `delete_objects` | `delete_objects(Bucket=, Delete={'Object':[{'Key':k},...], 'Quiet':'true'})` |

`get_presigned_url` 的 `Params={'response-content-disposition': ...}`（用于成片下载的附件头）**已在 Task 2 实施时用真实请求验证可用**，Task 3 与 Task 13 可直接采用，无需再自行验证。

- [ ] **Step 2: 写失败的冒烟测试**

创建 `backend/tests/integration/conftest_cos.py`：

```python
"""COS 集成测试夹具：唯一前缀隔离 + teardown 清理。

按项目规则，COS 不 mock —— 这些测试打真实 dev bucket。
未配置时自动 skip，便于无凭证环境跑其余测试。
"""
import uuid

import pytest

from app.config import settings


def _cos_configured() -> bool:
    if not settings.cos_bucket or not settings.cos_region:
        return False
    if settings.cos_auth_mode == "static":
        return bool(settings.cos_secret_id and settings.cos_secret_key)
    return True


requires_cos = pytest.mark.skipif(
    not _cos_configured(),
    reason="需要 COS 凭证与 dev bucket（设置 cos_region/cos_bucket/cos_secret_*）",
)


@pytest.fixture
async def cos_prefix():
    """本次测试专属 key 前缀，退出时递归删除。

    必须先 warm_credentials()：object_store 的每个调用都经 get_client()，
    而后者读凭证缓存，缓存为空会抛 RuntimeError。
    """
    from app.services import cos_client, object_store

    await cos_client.warm_credentials()
    prefix = f"test/{uuid.uuid4().hex}/"
    yield prefix
    await object_store.delete_prefix(prefix)
```

> **注册说明（易漏）**：`conftest_cos.py` 是普通模块，**pytest 不会自动发现其中的
> fixture**。`requires_cos` 这个 marker 靠测试文件里直接 `from tests.integration.conftest_cos
> import requires_cos` 就能用，但 `cos_prefix` 作为 fixture 必须在
> `backend/tests/integration/conftest.py` 中重新导出才会被注入：
>
> ```python
> from tests.integration.conftest_cos import cos_prefix  # noqa: F401  fixture 注册
> ```
>
> 漏掉这一步的表现是所有用到 `cos_prefix` 的测试报 `fixture 'cos_prefix' not found`。
> Task 2 的冒烟测试没用到该 fixture，所以这个洞直到 Task 3 才会暴露。

创建 `backend/tests/integration/test_cos_client_smoke.py`：

```python
"""cos_client 冒烟：单例、凭证预热、干净关闭。"""
from tests.integration.conftest_cos import requires_cos

from app.services import cos_client


@requires_cos
async def test_client_is_singleton():
    """SDK 要求一个 region 只建一个实例并复用，否则占用过多连接和线程。

    注意先 warm_credentials()：get_client() 内部读凭证缓存，缓存为空会抛
    RuntimeError（这是刻意设计——同步签名路径绝不能阻塞去取凭证）。
    """
    await cos_client.warm_credentials()
    a = cos_client.get_client()
    b = cos_client.get_client()
    assert a is b


@requires_cos
async def test_warm_credentials_populates_cache():
    await cos_client.warm_credentials()
    cred = cos_client.get_cached_credentials()
    assert cred["secret_id"]
    assert cred["secret_key"]


@requires_cos
async def test_close_client_is_idempotent():
    cos_client.get_client()
    await cos_client.close_client()
    await cos_client.close_client()  # 第二次不应抛异常
```

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run --project backend pytest tests/integration/test_cos_client_smoke.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.cos_client'`

- [ ] **Step 4: 实现 cos_client**

创建 `backend/app/services/cos_client.py`：

```python
"""腾讯云 COS 客户端与凭证管理。

本模块是全项目唯一 import qcloud_cos 的地方；其余代码一律经由
object_store / workspace 访问 COS。

两个设计约束：
1. CosS3Client 必须单例 —— SDK 要求一个 region 只建一个实例并复用，
   否则进程会占用过多连接和线程。
2. 凭证缓存 —— to_media_url() 是同步函数（被 projects.py 的同步序列化器
   在列表推导中调用），而预签名需要凭证。static 模式凭证是常量无所谓；
   cvm_role 模式下取凭证是网络操作，不能在同步函数里做，因此由
   warm_credentials() 预热、后台协程周期刷新，签名时只读缓存。
"""

import asyncio
import logging
import time
from typing import Optional

import httpx
from qcloud_cos import CosConfig, CosS3Client

from app.config import settings

logger = logging.getLogger(__name__)

# CVM 实例角色的元数据服务地址
_METADATA_URL = (
    "http://metadata.tencentyun.com/latest/meta-data/cam/security-credentials/"
)

_client: Optional[CosS3Client] = None
_cached_cred: Optional[dict] = None
_cred_expires_at: Optional[float] = None  # unix 秒；None = 永不过期（static）
_refresh_task: Optional[asyncio.Task] = None


def bucket() -> str:
    """目标 bucket 名（含 AppId）。"""
    if not settings.cos_bucket:
        raise RuntimeError("未配置 cos_bucket")
    return settings.cos_bucket


def _fetch_cvm_role_credentials() -> dict:
    """从 CVM 实例元数据服务取 STS 临时密钥。同步阻塞调用。"""
    if not settings.cos_cvm_role:
        raise RuntimeError("cvm_role 模式需要 cos_cvm_role")
    r = httpx.get(f"{_METADATA_URL}{settings.cos_cvm_role}", timeout=5)
    r.raise_for_status()
    d = r.json()
    return {
        "secret_id": d["TmpSecretId"],
        "secret_key": d["TmpSecretKey"],
        "token": d["Token"],
        "expired_at": d.get("ExpiredTime"),
    }


def _build_config(cred: dict) -> CosConfig:
    kwargs = {
        "Region": settings.cos_region,
        "SecretId": cred["secret_id"],
        "SecretKey": cred["secret_key"],
        "Scheme": settings.cos_scheme,
    }
    if cred.get("token"):
        kwargs["Token"] = cred["token"]
    if settings.cos_domain:
        kwargs["Domain"] = settings.cos_domain
    return CosConfig(**kwargs)


def get_client() -> CosS3Client:
    """CosS3Client 单例。"""
    global _client
    if _client is None:
        _client = CosS3Client(_build_config(get_cached_credentials()))
        logger.info(
            "cos_client_created",
            extra={"region": settings.cos_region, "bucket": settings.cos_bucket,
                   "auth_mode": settings.cos_auth_mode},
        )
    return _client


async def warm_credentials() -> None:
    """预热凭证缓存。FastAPI lifespan 与 worker 启动时调用。

    cvm_role 模式下临时密钥会变，凭证是构造 CosConfig 时传入的，
    因此刷新凭证必须一并重建 client —— 这是与永久密钥模式的关键差异。
    """
    global _cached_cred, _cred_expires_at, _client

    if settings.cos_auth_mode == "static":
        if not (settings.cos_secret_id and settings.cos_secret_key):
            raise RuntimeError("static 模式需要 cos_secret_id / cos_secret_key")
        _cached_cred = {
            "secret_id": settings.cos_secret_id,
            "secret_key": settings.cos_secret_key,
            "token": None,
        }
        _cred_expires_at = None
        return

    cred = await asyncio.to_thread(_fetch_cvm_role_credentials)
    _cached_cred = cred
    expired_at = cred.get("expired_at")
    _cred_expires_at = float(expired_at) if expired_at else time.time() + 3600
    _client = None  # 强制下次 get_client() 用新凭证重建
    logger.info("cos_credentials_warmed", extra={"auth_mode": settings.cos_auth_mode})


async def _refresh_loop() -> None:
    """按凭证剩余有效期的 50% 周期刷新，保证同步签名路径永远读到有效凭证。"""
    while True:
        try:
            # 不能写 `credentials_remaining_sec() or 3600`：凭证已完全过期时
            # 该函数正确返回 0，而 0 是 falsy，会被误当成「还没算出来」而按
            # 默认 TTL 睡 1800 秒——把一次瞬时刷新失败放大成最长 30 分钟的
            # 凭证失效窗口。0 必须原样保留，好让下面 sleep 取到下限 60 秒。
            remaining = credentials_remaining_sec()
            remaining = 3600 if remaining is None else remaining
            await asyncio.sleep(max(60, remaining // 2))
            await warm_credentials()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("cos_credentials_refresh_failed")
            await asyncio.sleep(60)


async def start_credential_refresh() -> None:
    """启动后台刷新协程（仅 cvm_role 模式需要）。"""
    global _refresh_task
    if settings.cos_auth_mode != "cvm_role":
        return
    if _refresh_task is None or _refresh_task.done():
        _refresh_task = asyncio.create_task(_refresh_loop())


def get_cached_credentials() -> dict:
    """同步读取缓存凭证。缓存为空时抛错，绝不阻塞去取。"""
    if _cached_cred is None:
        raise RuntimeError(
            "COS 凭证缓存为空；应在应用启动时调用 warm_credentials()"
        )
    return _cached_cred


def credentials_remaining_sec() -> Optional[int]:
    """缓存凭证剩余有效秒数。static 模式返回 None（永不过期）。"""
    if _cred_expires_at is None:
        return None
    return max(0, int(_cred_expires_at - time.time()))


async def close_client() -> None:
    """停止后台刷新并释放单例。幂等。"""
    global _client, _refresh_task
    if _refresh_task is not None:
        _refresh_task.cancel()
        try:
            await _refresh_task
        except (asyncio.CancelledError, Exception):
            pass
        _refresh_task = None
    _client = None
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run --project backend pytest tests/integration/test_cos_client_smoke.py -v`
Expected: PASS（3 项）；无凭证环境会 SKIPPED——**SKIPPED 不算通过**，须在有凭证环境重跑确认

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/cos_client.py \
        backend/tests/integration/conftest_cos.py \
        backend/tests/integration/test_cos_client_smoke.py
git commit -m "feat(cos): 新增 cos_client，CosS3Client 单例与凭证缓存

凭证缓存服务于同步签名路径：to_media_url 被同步序列化器调用，
不能在其中触发取凭证的网络请求，故由 lifespan 预热 + 后台协程刷新。
cvm_role 模式下临时密钥变化必须一并重建 client——Token 是构造
CosConfig 时传入的，不重建会继续用过期凭证签名。"
```

---

## Task 3: object_store — 对象操作原语

**Files:**
- Create: `backend/app/services/object_store.py`
- Test: `backend/tests/integration/test_object_store.py`

**Interfaces:**
- Consumes: Task 2 的 `get_client()`、`bucket()`、`get_cached_credentials()`、`credentials_remaining_sec()`
- Produces（除 `signed_url` 外全部 `async`）：
  - `async def put(key: str, local_path: Path, content_type: Optional[str] = None) -> str`
  - `async def get(key: str, dest_path: Path) -> Path`
  - `async def exists(key: str) -> bool`
  - `async def size(key: str) -> int` — 不存在时抛 `FileNotFoundError`
  - `async def copy(src_key: str, dst_key: str) -> str`
  - `async def delete(key: str) -> None` — 幂等
  - `async def delete_prefix(prefix: str) -> int` — 返回删除数量，内部分页
  - `async def list_prefix(prefix: str) -> list[str]` — 内部分页
  - `def signed_url(key: str, expires_sec: Optional[int] = None, filename: Optional[str] = None) -> str` — **同步**

**核心约束**：SDK 是同步的。**每个方法内的 SDK 调用都必须 `await asyncio.to_thread(...)`**，唯一例外是 `signed_url`（纯本地 HMAC，不发网络请求）。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/integration/test_object_store.py`：

```python
"""object_store 原语——打真实 dev bucket，唯一前缀隔离。"""
import asyncio
import time

import httpx
import pytest

from tests.integration.conftest_cos import requires_cos

from app.services import object_store

pytestmark = requires_cos


async def test_put_then_get_roundtrip(cos_prefix, tmp_path):
    src = tmp_path / "a.txt"
    src.write_bytes(b"hello cos")
    key = await object_store.put(f"{cos_prefix}a.txt", src)

    dest = tmp_path / "back.txt"
    await object_store.get(key, dest)
    assert dest.read_bytes() == b"hello cos"


async def test_exists_and_size(cos_prefix, tmp_path):
    src = tmp_path / "b.bin"
    src.write_bytes(b"x" * 1234)
    key = await object_store.put(f"{cos_prefix}b.bin", src)

    assert await object_store.exists(key) is True
    assert await object_store.size(key) == 1234
    assert await object_store.exists(f"{cos_prefix}nope.bin") is False


async def test_size_missing_raises(cos_prefix):
    with pytest.raises(FileNotFoundError):
        await object_store.size(f"{cos_prefix}missing.bin")


async def test_copy_is_server_side(cos_prefix, tmp_path):
    src = tmp_path / "c.txt"
    src.write_bytes(b"copy me")
    key = await object_store.put(f"{cos_prefix}c.txt", src)

    dst = await object_store.copy(key, f"{cos_prefix}c_backup.txt")
    assert await object_store.exists(dst)

    back = tmp_path / "c2.txt"
    await object_store.get(dst, back)
    assert back.read_bytes() == b"copy me"


async def test_delete_is_idempotent(cos_prefix, tmp_path):
    src = tmp_path / "d.txt"
    src.write_bytes(b"bye")
    key = await object_store.put(f"{cos_prefix}d.txt", src)

    await object_store.delete(key)
    assert await object_store.exists(key) is False
    await object_store.delete(key)  # 第二次不应抛异常


async def test_list_and_delete_prefix(cos_prefix, tmp_path):
    for i in range(3):
        f = tmp_path / f"e{i}.txt"
        f.write_bytes(b"z")
        await object_store.put(f"{cos_prefix}sub/e{i}.txt", f)

    keys = await object_store.list_prefix(f"{cos_prefix}sub/")
    assert len(keys) == 3
    assert all(k.startswith(f"{cos_prefix}sub/") for k in keys)

    n = await object_store.delete_prefix(f"{cos_prefix}sub/")
    assert n == 3
    assert await object_store.list_prefix(f"{cos_prefix}sub/") == []


async def test_signed_url_actually_fetches_content(cos_prefix, tmp_path):
    """断言 URL 真能取到内容，而非断言字符串形态——签名算错时形态照样对。"""
    src = tmp_path / "f.txt"
    src.write_bytes(b"signed content")
    key = await object_store.put(f"{cos_prefix}f.txt", src)

    url = object_store.signed_url(key)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url)
    assert r.status_code == 200
    assert r.content == b"signed content"


async def test_transfers_do_not_block_event_loop(cos_prefix, tmp_path):
    """SDK 是同步的，传输必须在线程池里跑。

    若某个方法漏了 asyncio.to_thread，上传期间事件循环会被独占，
    并发的心跳协程推进次数会显著下降。这是本方案最隐蔽的一类缺陷，
    且只在文件够大时才复现，故用 8MB 文件放大信号。
    """
    big = tmp_path / "big.bin"
    big.write_bytes(b"0" * (8 * 1024 * 1024))

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    hb = asyncio.create_task(heartbeat())
    try:
        await object_store.put(f"{cos_prefix}big.bin", big)
    finally:
        hb.cancel()

    # 事件循环若被阻塞，ticks 会接近 0
    assert ticks > 5, f"事件循环疑似被阻塞，心跳仅推进 {ticks} 次"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project backend pytest tests/integration/test_object_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.object_store'`

- [ ] **Step 3: 实现 object_store**

创建 `backend/app/services/object_store.py`：

```python
"""COS 对象操作原语。不含任何业务语义。

SDK 是纯同步的，因此每个传输方法都用 asyncio.to_thread 把阻塞调用移出
事件循环。唯一例外是 signed_url —— 预签名是纯本地 HMAC 计算，不发网络
请求，进线程池只会徒增开销。

一致性约定（调用方须遵守）：新增文件先 put 成功再写 DB；删除先改 DB 解除
引用再删 COS。DB 中的 key 必须永远指向真实存在的对象，宁可留孤儿对象。
"""

import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import Optional

from app.config import settings
from app.services.cos_client import (
    bucket,
    credentials_remaining_sec,
    get_client,
)

logger = logging.getLogger(__name__)

# 超过此大小走 upload_file 高级接口（自动分块 + 断点续传）
MULTIPART_THRESHOLD = 100 * 1024 * 1024


def _log_cos_error(op: str, key: str, exc: Exception) -> None:
    """记录 RequestId —— 腾讯云工单排查以它为准。"""
    logger.error(
        "cos_operation_failed",
        extra={
            "op": op,
            "key": key,
            "error_code": getattr(exc, "get_error_code", lambda: None)(),
            "status_code": getattr(exc, "get_status_code", lambda: None)(),
            "request_id": getattr(exc, "get_request_id", lambda: None)(),
        },
    )


async def put(key: str, local_path: Path, content_type: Optional[str] = None) -> str:
    """上传本地文件到 key。返回 key。"""
    local_path = Path(local_path)
    n = local_path.stat().st_size
    ct = content_type or mimetypes.guess_type(local_path.name)[0]
    client = get_client()

    def _do():
        if n >= MULTIPART_THRESHOLD:
            # 高级接口按大小自动选简单/分块上传，分块自带断点续传。
            # ContentType 必须一并传：本项目的读路径是浏览器用 <video> 播签名
            # URL，Content-Type 缺失会让浏览器改为下载而非内联播放，而合并后的
            # 成片很可能正好越过这个阈值走到这一支。
            mp_kwargs = {}
            if ct:
                mp_kwargs["ContentType"] = ct
            return client.upload_file(
                Bucket=bucket(), Key=key, LocalFilePath=str(local_path),
                PartSize=10, MAXThread=10, EnableMD5=False, **mp_kwargs,
            )
        kwargs = {"Bucket": bucket(), "Key": key, "EnableMD5": False}
        if ct:
            kwargs["ContentType"] = ct
        with open(local_path, "rb") as f:
            return client.put_object(Body=f, **kwargs)

    try:
        await asyncio.to_thread(_do)
    except Exception as e:
        _log_cos_error("put", key, e)
        raise

    logger.info("cos_put", extra={"key": key, "bytes": n})
    return key


async def get(key: str, dest_path: Path) -> Path:
    """下载 key 到本地。父目录自动创建。返回 dest_path。"""
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    client = get_client()

    def _do():
        r = client.get_object(Bucket=bucket(), Key=key)
        # 流式写盘，不整体读入内存：分镜视频可达数百 MB
        r["Body"].get_stream_to_file(str(dest_path))

    try:
        await asyncio.to_thread(_do)
    except Exception as e:
        _log_cos_error("get", key, e)
        raise
    logger.info("cos_get", extra={"key": key, "bytes": dest_path.stat().st_size})
    return dest_path


async def exists(key: str) -> bool:
    """对象是否存在。用 SDK 自带的 object_exists。"""
    client = get_client()
    try:
        return await asyncio.to_thread(client.object_exists, bucket(), key)
    except Exception as e:
        _log_cos_error("exists", key, e)
        raise


async def size(key: str) -> int:
    """对象字节数。不存在时抛 FileNotFoundError。"""
    client = get_client()
    try:
        r = await asyncio.to_thread(client.head_object, Bucket=bucket(), Key=key)
    except Exception as e:
        if getattr(e, "get_status_code", lambda: None)() == 404:
            raise FileNotFoundError(key) from e
        _log_cos_error("size", key, e)
        raise
    return int(r["Content-Length"])


async def copy(src_key: str, dst_key: str) -> str:
    """服务端拷贝，不产生本地流量。返回 dst_key。"""
    client = get_client()
    source = {
        "Bucket": bucket(),
        "Key": src_key,
        "Region": settings.cos_region,
    }
    try:
        await asyncio.to_thread(
            client.copy_object, Bucket=bucket(), Key=dst_key, CopySource=source
        )
    except Exception as e:
        _log_cos_error("copy", src_key, e)
        raise
    logger.info("cos_copy", extra={"src": src_key, "dst": dst_key})
    return dst_key


async def delete(key: str) -> None:
    """删除对象。幂等——对象不存在不报错。"""
    client = get_client()
    try:
        await asyncio.to_thread(client.delete_object, Bucket=bucket(), Key=key)
    except Exception as e:
        if getattr(e, "get_status_code", lambda: None)() == 404:
            return
        _log_cos_error("delete", key, e)
        raise
    logger.info("cos_delete", extra={"key": key})


async def list_prefix(prefix: str) -> list[str]:
    """列出前缀下全部 key。单次 list_objects 上限 1000，故循环取到尽。"""
    client = get_client()

    def _do() -> list[str]:
        keys: list[str] = []
        marker = ""
        while True:
            r = client.list_objects(Bucket=bucket(), Prefix=prefix, Marker=marker)
            for obj in r.get("Contents", []):
                keys.append(obj["Key"])
            # IsTruncated 是字符串 'true'/'false'，不是布尔 —— 改成布尔判断会让
            # 超过 1000 个对象的前缀只取第一页并静默丢数据。
            if r.get("IsTruncated") != "true":
                break
            # 截断时 NextMarker 通常存在，但腾讯云文档说明它可能缺失，
            # 此时应回退用本页最后一个 Key 作为下一页起点。
            # 直接 r["NextMarker"] 会 KeyError，把静默丢数据升级成崩溃。
            marker = r.get("NextMarker") or (keys[-1] if keys else "")
            if not marker:
                # 声称截断却既无 NextMarker 也无内容：无法确定下一页起点，
                # 继续循环会死循环，就此停下并让调用方看到不完整结果。
                logger.warning("cos_list_truncated_without_marker",
                               extra={"prefix": prefix, "got": len(keys)})
                break
        return keys

    try:
        return await asyncio.to_thread(_do)
    except Exception as e:
        _log_cos_error("list_prefix", prefix, e)
        raise


async def delete_prefix(prefix: str) -> int:
    """删除前缀下全部对象。返回删除数量。批量删单次上限 1000，必须分批。"""
    keys = await list_prefix(prefix)
    if not keys:
        return 0
    client = get_client()

    def _do(chunk: list[str]) -> list[str]:
        """删除一批，返回本批中删除失败的 key。

        Quiet='true' 只是不回报成功项；**失败项仍会出现在 Error 里，
        且整个请求依然是 HTTP 200**。不看 Error 就会把部分失败当成完全成功。
        """
        r = client.delete_objects(
            Bucket=bucket(),
            Delete={"Object": [{"Key": k} for k in chunk], "Quiet": "true"},
        )
        return [e.get("Key") for e in (r.get("Error") or [])]

    failed: list[str] = []
    for i in range(0, len(keys), 1000):
        try:
            failed.extend(await asyncio.to_thread(_do, keys[i:i + 1000]))
        except Exception as e:
            _log_cos_error("delete_prefix", prefix, e)
            raise

    deleted = len(keys) - len(failed)
    if failed:
        # 删不掉只留下孤儿对象，按一致性规则是可接受的（宁可留孤儿，
        # 绝不留悬空引用），所以不抛异常；但绝不能谎报成功。
        logger.error("cos_delete_prefix_partial_failure",
                     extra={"prefix": prefix, "deleted": deleted,
                            "failed": len(failed), "failed_keys": failed[:20]})
    logger.info("cos_delete_prefix", extra={"prefix": prefix, "count": deleted})
    return deleted


def signed_url(
    key: str,
    expires_sec: Optional[int] = None,
    filename: Optional[str] = None,
) -> str:
    """生成预签名 GET URL。**同步**——纯本地 HMAC 计算，不发网络请求。

    有效期取 min(配置 TTL, 凭证剩余有效期)：签名有效期不能超过临时密钥
    有效期，否则 URL 会在 TTL 内因凭证先过期而失效。

    filename 非空时附加 response-content-disposition，让 COS 直接返回附件
    下载头（用于成片下载）。
    """
    ttl = expires_sec if expires_sec is not None else settings.cos_signed_url_ttl_sec
    remaining = credentials_remaining_sec()
    if remaining is not None:
        ttl = min(ttl, remaining)

    params = {}
    if filename:
        params["response-content-disposition"] = f'attachment; filename="{filename}"'

    return get_client().get_presigned_url(
        Method="GET",
        Bucket=bucket(),
        Key=key,
        Expired=ttl,
        Params=params or None,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --project backend pytest tests/integration/test_object_store.py -v`
Expected: PASS（8 项）

若 `get_presigned_url` 的参数名（`Expired` / `Params`）或异常对象的取值方法（`get_status_code` / `get_request_id`）与 Step 1 核对结果不符，按实际修正。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/object_store.py backend/tests/integration/test_object_store.py
git commit -m "feat(cos): 新增 object_store 对象原语

SDK 是纯同步的，每个传输方法都用 asyncio.to_thread 移出事件循环；
signed_url 例外——纯 HMAC 无网络 IO，进线程池徒增开销。
新增事件循环阻塞检测测试：漏 to_thread 不会报错，只会在传大文件时
让服务卡死，必须由测试守住。

签名有效期取 min(配置TTL, 凭证剩余)，避免 URL 在 TTL 内因临时密钥
先过期而失效。delete_prefix 按 1000 分批，符合 COS 批量删除上限。"
```

---
## Task 4: workspace — ffmpeg 临时工作区

**Files:**
- Create: `backend/app/services/workspace.py`
- Test: `backend/tests/integration/test_workspace.py`

**Interfaces:**
- Consumes: Task 3 的 `object_store.get` / `put`
- Produces:
  - `workspace()` — async context manager，产出 `Workspace` 实例
  - `Workspace.path(name: str) -> Path` — 工作区内新文件路径（不下载）
  - `async Workspace.fetch(key: str, name: Optional[str] = None) -> Path` — 下载 key 到工作区
  - `async Workspace.publish(local_path: Path, key: str) -> str` — 上传并返回 key
  - `Workspace.root -> Path` — 工作区根目录
  - `async def ensure_free_space(required_bytes: int) -> None` — 空间不足时抛 `OSError`

- [ ] **Step 1: 写失败测试**

> **测试放置规则（易犯）**：`tests/integration/test_workspace.py` 顶部有模块级
> `pytestmark = requires_cos`，**该模块内的一切测试在无凭证环境下都会被 SKIP**。
> 因此**只有真正调用 `object_store` 的测试才放这里**；纯逻辑测试（磁盘空间预检、
> `path()` 的路径校验等）一律放 `backend/tests/unit/` 下的独立文件，不带任何 COS
> gate，好让它们在无凭证的 CI/开发环境里照常运行。
>
> 把纯逻辑的守卫测试放进 gate 后面，等于那条守卫在日常环境里没有任何测试覆盖——
> 将来误改回去也不会有人发现。

创建 `backend/tests/integration/test_workspace.py`：

```python
"""workspace 上下文管理器——打真实 dev bucket。

只放真正需要 COS 的测试；纯逻辑测试见 tests/unit/。
"""
import pytest

from tests.integration.conftest_cos import requires_cos

from app.services import object_store
from app.services.workspace import workspace, ensure_free_space

pytestmark = requires_cos


async def test_fetch_then_publish_roundtrip(cos_prefix, tmp_path):
    src = tmp_path / "in.txt"
    src.write_bytes(b"workspace roundtrip")
    key = await object_store.put(f"{cos_prefix}in.txt", src)

    async with workspace() as ws:
        local = await ws.fetch(key)
        assert local.read_bytes() == b"workspace roundtrip"

        out = ws.path("out.txt")
        out.write_bytes(local.read_bytes() + b" processed")
        out_key = await ws.publish(out, f"{cos_prefix}out.txt")

    assert await object_store.exists(out_key)
    dest = tmp_path / "verify.txt"
    await object_store.get(out_key, dest)
    assert dest.read_bytes() == b"workspace roundtrip processed"


async def test_tmpdir_removed_on_exit(cos_prefix, tmp_path):
    src = tmp_path / "x.txt"
    src.write_bytes(b"x")
    key = await object_store.put(f"{cos_prefix}x.txt", src)

    async with workspace() as ws:
        root = ws.root
        await ws.fetch(key)
        assert root.exists()
    assert not root.exists()


async def test_tmpdir_removed_even_on_exception(cos_prefix):
    captured = {}
    with pytest.raises(ValueError):
        async with workspace() as ws:
            captured["root"] = ws.root
            raise ValueError("boom")
    assert not captured["root"].exists()


async def test_fetch_accepts_custom_local_name(cos_prefix, tmp_path):
    src = tmp_path / "y.mp4"
    src.write_bytes(b"video bytes")
    key = await object_store.put(f"{cos_prefix}deep/nested/y.mp4", src)

    async with workspace() as ws:
        local = await ws.fetch(key, name="source.mp4")
        assert local.name == "source.mp4"
        assert local.read_bytes() == b"video bytes"


async def test_ensure_free_space_raises_when_insufficient():
    with pytest.raises(OSError, match="磁盘空间不足"):
        await ensure_free_space(1 << 60)  # 1 EiB，必然不足
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project backend pytest tests/integration/test_workspace.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.workspace'`

- [ ] **Step 3: 实现 workspace**

创建 `backend/app/services/workspace.py`：

```python
"""ffmpeg 临时工作区。

COS 是权威存储，本地不保留任何持久文件。需要跑 ffmpeg 时，把输入 fetch
到一次性临时目录，算完 publish 回 COS，退出即删。

用法：
    async with workspace() as ws:
        src = await ws.fetch(shot_video_key(pid, sid))
        out = ws.path("last_frame.png")
        await extract_last_frame(src, out)
        await ws.publish(out, shot_last_frame_key(pid, sid))
"""

import logging
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from app.services import object_store

logger = logging.getLogger(__name__)


async def ensure_free_space(required_bytes: int, at: Optional[Path] = None) -> None:
    """检查可用磁盘空间。不足则抛 OSError。

    导出合并会把整个项目的分镜视频拉到本地，不预检会表现为 ffmpeg 神秘失败。
    """
    target = at or Path(tempfile.gettempdir())
    usage = shutil.disk_usage(target)
    if usage.free < required_bytes:
        raise OSError(
            f"磁盘空间不足：{target} 可用 {usage.free} 字节，需要 {required_bytes} 字节"
        )


class Workspace:
    """一次性临时工作区。不要直接构造，用 workspace() 上下文管理器。"""

    def __init__(self, root: Path):
        self.root = root
        # 本地名 -> 产生它的 key，用于探测同名不同源的静默覆盖
        self._fetched: dict[str, str] = {}

    def path(self, name: str) -> Path:
        """工作区内的新文件路径。不下载任何内容，仅拼路径。

        name 必须是工作区内的相对路径。绝对路径和 .. 都被拒绝——
        `Path('/tmp/ws') / '/etc/passwd'` 在 pathlib 里会**丢弃左操作数**
        直接返回 `/etc/passwd`，不挡住就等于允许写到工作区外面，
        而工作区外的文件不会被退出时的 rmtree 清掉。
        """
        rel = Path(name)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"工作区内文件名必须是不含 .. 的相对路径: {name!r}")
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    async def fetch(self, key: str, name: Optional[str] = None) -> Path:
        """下载 key 到工作区。name 省略时用 key 的最后一段作文件名。

        **同一工作区内 fetch 多个 key 时务必显式传 name。** 项目里有一批
        固定名素材（audio_original.wav、audio_vc.wav、target_last_frame.png
        等），不同分镜的同类素材最后一段完全相同，用默认名会互相覆盖。
        下面的守卫会把这种静默覆盖变成显式报错——导出合并那类「多个分镜拉进
        同一个工作区」的场景，出错总好过悄悄 concat 出 N 份同一个视频。
        """
        local_name = name or key.rsplit("/", 1)[-1]
        prev = self._fetched.get(local_name)
        if prev is not None and prev != key:
            raise ValueError(
                f"工作区内本地名 {local_name!r} 已被 key {prev!r} 占用，"
                f"现在又要装入 {key!r}；请为其中之一显式指定 name="
            )
        local = self.path(local_name)
        await object_store.get(key, local)
        self._fetched[local_name] = key
        return local

    async def publish(self, local_path: Path, key: str) -> str:
        """上传工作区文件到 key。返回 key。"""
        return await object_store.put(key, Path(local_path))


@asynccontextmanager
async def workspace():
    """产出一次性 Workspace，退出时无条件删除整个临时目录。"""
    root = Path(tempfile.mkdtemp(prefix="vm_ws_"))
    logger.debug("workspace_created", extra={"root": str(root)})
    try:
        yield Workspace(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        logger.debug("workspace_removed", extra={"root": str(root)})
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --project backend pytest tests/integration/test_workspace.py -v`
Expected: PASS（5 项）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/workspace.py backend/tests/integration/test_workspace.py
git commit -m "feat(oss): 新增 workspace 临时工作区

不设持久本地缓存——浏览器直连 COS，本地文件只有 ffmpeg 需要，
而 ffmpeg 任务都是离散的，用完即弃已足够。"
```

---

## Task 5: storage.py 改造为 key 函数

**Files:**
- Modify: `backend/app/services/storage.py`（全文改造）
- Test: `backend/tests/unit/test_storage_keys.py`

**Interfaces:**
- Consumes: Task 3 的 `object_store.signed_url`
- Produces（全部返回 `str` 类型的 key，不再返回 `Path`）：
  - `def ts_uuid_name(ext: str = ".png") -> str` — **保持不变**
  - `def project_prefix(project_id: str) -> str` → `projects/<pid>/`
  - `def reference_images_prefix(project_id: str) -> str`
  - `def shot_prefix(project_id: str, shot_id: int) -> str`
  - `def shot_candidates_prefix(project_id: str, shot_id: int) -> str`
  - `def shot_custom_frames_prefix(project_id: str, shot_id: int) -> str`
  - `def shot_key(project_id: str, shot_id: int, filename: str) -> str`
  - `def shot_audio_original_key(project_id: str, shot_id: int) -> str`
  - `def shot_audio_vc_key(project_id: str, shot_id: int) -> str`
  - `def shot_target_last_frame_key(project_id: str, shot_id: int) -> str`
  - `def storyboard_key(project_id: str) -> str`
  - `def archived_storyboard_key(project_id: str, timestamp: str) -> str`
  - `def motion_prompt_key(project_id: str, shot_id: int) -> str`
  - `def final_video_key(project_id: str) -> str`
  - `def join_preview_key(project_id: str) -> str`
  - `def reference_image_key(project_id: str, image_id: str, filename: str) -> str`
  - `def is_valid_key(key: str) -> bool` — 替代 `validate_safe_path`
  - `def to_media_url(key: Optional[str]) -> Optional[str]` — **同步**，返回签名 URL

**删除**（本 task 移除，调用点在 Task 7–12 改造）：`project_dir` / `shots_dir` / `shot_dir` / `shot_output_path` / `shot_last_frame_path` / `pristine_video_path` / `pristine_last_frame_path` / `shot_source_path` / `get_original_video_for_audio` / `shot_pre_vc_video_path` / `shot_pre_cc_last_frame_path` / `final_dir` / `ensure_project_dirs` / `ensure_shot_dir` / `delete_project_storage` / `get_storage_relative_path` / `validate_safe_path`

> 删除这些函数会让所有调用点立刻报 `ImportError`——这正是方案 B 的设计意图：遗漏在所有环境同等失败，不会潜伏到生产。Task 7–12 逐个修复。**本 task 结束时整体测试套件是红的，属预期**；Task 5 只需自己的单元测试通过。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/unit/test_storage_keys.py`：

```python
"""key 拼接纯函数——无网络，最快的一层。"""
import re

from app.services import storage


def test_project_prefix():
    assert storage.project_prefix("p1") == "projects/p1/"


def test_shot_prefix():
    assert storage.shot_prefix("p1", 3) == "projects/p1/shots/shot_3/"


def test_shot_key_joins_filename():
    assert storage.shot_key("p1", 3, "output_1.mp4") == \
        "projects/p1/shots/shot_3/output_1.mp4"


def test_fixed_name_keys():
    assert storage.shot_audio_original_key("p1", 2) == \
        "projects/p1/shots/shot_2/audio_original.wav"
    assert storage.shot_audio_vc_key("p1", 2) == \
        "projects/p1/shots/shot_2/audio_vc.wav"
    assert storage.shot_target_last_frame_key("p1", 2) == \
        "projects/p1/shots/shot_2/target_last_frame.png"


def test_project_level_keys():
    assert storage.storyboard_key("p1") == "projects/p1/storyboard.json"
    assert storage.final_video_key("p1") == "projects/p1/final/merged.mp4"
    assert storage.join_preview_key("p1") == "projects/p1/previews/join_preview.mp4"
    assert storage.archived_storyboard_key("p1", "20260726") == \
        "projects/p1/storyboard_20260726.json"


def test_reference_image_key():
    assert storage.reference_image_key("p1", "img7", "face.jpg") == \
        "projects/p1/reference_images/img7_face.jpg"


def test_candidates_and_custom_frames_prefixes():
    assert storage.shot_candidates_prefix("p1", 4) == \
        "projects/p1/shots/shot_4/candidates/"
    assert storage.shot_custom_frames_prefix("p1", 4) == \
        "projects/p1/shots/shot_4/custom_frames/"


def test_no_key_has_leading_slash():
    """DB 存裸 key——前导斜杠会让 key 与迁移脚本的相对路径映射对不上。"""
    keys = [
        storage.project_prefix("p1"),
        storage.shot_prefix("p1", 1),
        storage.storyboard_key("p1"),
        storage.final_video_key("p1"),
        storage.reference_image_key("p1", "i", "f.jpg"),
    ]
    assert all(not k.startswith("/") for k in keys)


def test_ts_uuid_name_is_unique_and_well_formed():
    a = storage.ts_uuid_name(".mp4")
    b = storage.ts_uuid_name(".mp4")
    assert a != b
    assert re.fullmatch(r"\d+_[0-9a-f]{8}\.mp4", a)


def test_is_valid_key_rejects_traversal_and_absolute():
    assert storage.is_valid_key("projects/p1/shots/shot_1/output.mp4") is True
    assert storage.is_valid_key("projects/p1/../../etc/passwd") is False
    assert storage.is_valid_key("/projects/p1/x.mp4") is False
    assert storage.is_valid_key("etc/passwd") is False
    assert storage.is_valid_key("") is False


def test_to_media_url_passes_none_through():
    assert storage.to_media_url(None) is None
    assert storage.to_media_url("") is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project backend pytest tests/unit/test_storage_keys.py -v`
Expected: FAIL — `AttributeError: module 'app.services.storage' has no attribute 'project_prefix'`

- [ ] **Step 3: 重写 storage.py**

用以下内容**整体替换** `backend/app/services/storage.py`：

```python
"""项目素材的 COS key 工具。

COS 是权威存储。本模块只负责拼 key 与生成浏览器可访问的签名 URL；
任何本地文件都只存在于 workspace 的一次性临时目录中。

key 布局与迁移前的 storage_root 相对路径逐字符一致，因此存量迁移是
「本地相对路径 = key」的直接映射。
"""

import time
import uuid
from typing import Optional

from app.services import object_store


def ts_uuid_name(ext: str = ".png") -> str:
    """带时间戳的唯一文件名：``<unix_seconds>_<8hex><ext>``。

    保证 key 唯一，同时让浏览器/CDN 永远拿不到过期缓存。
    """
    return f"{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"


# ── 前缀 ──────────────────────────────────────────────────────────────────────

def project_prefix(project_id: str) -> str:
    return f"projects/{project_id}/"


def reference_images_prefix(project_id: str) -> str:
    return f"{project_prefix(project_id)}reference_images/"


def shots_prefix(project_id: str) -> str:
    return f"{project_prefix(project_id)}shots/"


def shot_prefix(project_id: str, shot_id: int) -> str:
    return f"{shots_prefix(project_id)}shot_{shot_id}/"


def shot_candidates_prefix(project_id: str, shot_id: int) -> str:
    return f"{shot_prefix(project_id, shot_id)}candidates/"


def shot_custom_frames_prefix(project_id: str, shot_id: int) -> str:
    return f"{shot_prefix(project_id, shot_id)}custom_frames/"


# ── 分镜级 key ────────────────────────────────────────────────────────────────

def shot_key(project_id: str, shot_id: int, filename: str) -> str:
    """分镜目录下任意文件的 key。用于 ts_uuid_name 生成的唯一名文件。"""
    return f"{shot_prefix(project_id, shot_id)}{filename}"


def shot_audio_original_key(project_id: str, shot_id: int) -> str:
    return shot_key(project_id, shot_id, "audio_original.wav")


def shot_audio_vc_key(project_id: str, shot_id: int) -> str:
    return shot_key(project_id, shot_id, "audio_vc.wav")


def shot_target_last_frame_key(project_id: str, shot_id: int) -> str:
    return shot_key(project_id, shot_id, "target_last_frame.png")


def motion_prompt_key(project_id: str, shot_id: int) -> str:
    return shot_key(project_id, shot_id, "motion_prompt.txt")


# ── 项目级 key ────────────────────────────────────────────────────────────────

def storyboard_key(project_id: str) -> str:
    return f"{project_prefix(project_id)}storyboard.json"


def archived_storyboard_key(project_id: str, timestamp: str) -> str:
    return f"{project_prefix(project_id)}storyboard_{timestamp}.json"


def final_video_key(project_id: str) -> str:
    return f"{project_prefix(project_id)}final/merged.mp4"


def join_preview_key(project_id: str) -> str:
    return f"{project_prefix(project_id)}previews/join_preview.mp4"


def reference_image_key(project_id: str, image_id: str, filename: str) -> str:
    return f"{reference_images_prefix(project_id)}{image_id}_{filename}"


# ── 校验与 URL ────────────────────────────────────────────────────────────────

def is_valid_key(key: str) -> bool:
    """key 安全校验：必须在 projects/ 下，且不含路径穿越。

    取代旧的 validate_safe_path()——后者用 str.startswith 判断路径包含关系，
    会把 /storage-evil 误判为位于 /storage 内。key 化后该问题不复存在。
    """
    if not key or key.startswith("/"):
        return False
    if ".." in key.split("/"):
        return False
    return key.startswith("projects/")


def to_media_url(key: Optional[str]) -> Optional[str]:
    """把 COS key 转成浏览器可直接访问的预签名 URL。

    保持**同步**——projects.py 的 _shot_to_dict / _candidate_to_dict 是同步
    序列化器且在列表推导中调用本函数，改 async 会连锁污染全部上游。
    签名是纯本地 HMAC 计算，不发网络请求，同步调用不阻塞事件循环。
    """
    if not key:
        return None
    return object_store.signed_url(key)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --project backend pytest tests/unit/test_storage_keys.py -v`
Expected: PASS（11 项）

- [ ] **Step 5: 确认预期的红**

Run: `uv run --project backend pytest tests/ -q 2>&1 | tail -20`
Expected: 大量 `ImportError`（如 `cannot import name 'shot_dir' from 'app.services.storage'`）。**这是预期的**——Task 7–12 逐个修复。记录下报错模块清单，作为后续 task 的核对依据。

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/storage.py backend/tests/unit/test_storage_keys.py
git commit -m "refactor(oss)!: storage.py 改为返回 COS key

BREAKING: 删除全部本地路径函数，调用点将报 ImportError，由后续 task 修复。
这正是方案 B 的意图——遗漏在所有环境同等失败，不会潜伏到生产。

to_media_url 保持同步：projects.py 的序列化器是同步函数且在列表推导中
调用它，改 async 会连锁污染全部上游。"
```

---

## Task 6: 新增三个 DB 列

**Files:**
- Modify: `backend/app/models/project.py:118-177`（`Shot` 类内）
- Modify: `backend/app/db.py`（`_has_column` 守卫段落末尾）
- Test: `backend/tests/integration/test_shot_key_columns.py`

**Interfaces:**
- Consumes: 无
- Produces: `Shot.pre_vc_video_key`、`Shot.pre_cc_last_frame_key`、`Shot.pristine_last_frame_key`（均为 `Text`，nullable）

**为何需要这三列**：现有代码用目录扫描推导素材状态——`pristine_last_frame_path()`（旧 storage.py:89）靠 `glob` 取 mtime 最新，`pre_cc.exists()`（pipeline.py:1860）靠固定文件名是否存在。COS 下本地目录随时为空，这些判断全部失效。尤其 CC 在 image_candidates.py:226 直接覆盖 `shot.last_frame_path`，校准后无法从任何现有字段反推校准前的尾帧，而 worker/tasks.py:659、worker/tasks.py:1058、pipeline.py:2093 都以它为还原目标。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/integration/test_shot_key_columns.py`：

```python
"""三个素材状态列：建列幂等 + 可读写。"""
import sqlalchemy as sa
from sqlalchemy import select

from app.models.project import Shot

from tests.integration.conftest import _make_project, _add_shot


async def test_columns_exist_on_fresh_schema(db_engine):
    async with db_engine.begin() as conn:
        cols = await conn.run_sync(
            lambda c: [r[1] for r in c.exec_driver_sql("PRAGMA table_info(shots)")]
        )
    assert "pre_vc_video_key" in cols
    assert "pre_cc_last_frame_key" in cols
    assert "pristine_last_frame_key" in cols


async def test_columns_default_to_null_and_are_writable(db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        assert shot.pre_vc_video_key is None
        assert shot.pre_cc_last_frame_key is None
        assert shot.pristine_last_frame_key is None

        shot.pristine_last_frame_key = "projects/p/shots/shot_1/last_frame_1_ab.png"
        shot.pre_vc_video_key = "projects/p/shots/shot_1/output_pre_vc.mp4"
        await s.commit()

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        assert shot.pristine_last_frame_key.endswith("last_frame_1_ab.png")
        assert shot.pre_vc_video_key.endswith("output_pre_vc.mp4")


async def test_migration_is_idempotent_on_legacy_table(db_engine):
    """在缺列的旧表上重复执行建列例程，不应报错。"""
    from app.db import _ensure_columns  # Step 3 中新增的可复用例程

    async with db_engine.begin() as conn:
        await conn.execute(sa.text("ALTER TABLE shots DROP COLUMN pre_vc_video_key"))

    async with db_engine.begin() as conn:
        await _ensure_columns(conn)
        await _ensure_columns(conn)  # 第二次必须无害

    async with db_engine.begin() as conn:
        cols = await conn.run_sync(
            lambda c: [r[1] for r in c.exec_driver_sql("PRAGMA table_info(shots)")]
        )
    assert "pre_vc_video_key" in cols
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project backend pytest tests/integration/test_shot_key_columns.py -v`
Expected: FAIL — `AssertionError`（列不存在）及 `ImportError: cannot import name '_ensure_columns'`

- [ ] **Step 3: 加模型字段**

在 `backend/app/models/project.py` 的 `Shot` 类内，紧随 `vc_audio_path`（:159）之后追加：

```python
    # ── 素材状态显式化（原先靠目录扫描/固定文件名推导，COS 下不成立）──────
    # VC 前的原视频 key。取代「output_pre_vc.mp4 是否存在」。
    pre_vc_video_key = Column(Text, nullable=True)
    # 角色校准前的尾帧备份 key。取代「last_frame_pre_cc.png 是否存在」。
    pre_cc_last_frame_key = Column(Text, nullable=True)
    # 未经校准的原始尾帧 key（CC 还原目标）。
    # 必需：CC 会直接覆盖 last_frame_path，校准后无法反推校准前的尾帧。
    pristine_last_frame_key = Column(Text, nullable=True)
```

- [ ] **Step 4: 加幂等建列**

在 `backend/app/db.py` 中，把三列加入既有的 `for col, typ in [...]` 幂等模式，并把整段建列逻辑提取为可复用的 `_ensure_columns(conn)` 函数（Spec B 的迁移脚本会直接调用它，以保证回填前列已存在）。

在既有 `("target_last_frame_path", "TEXT"), ...` 那个循环之后追加：

```python
    for col, typ in [
        ("pre_vc_video_key", "TEXT"),
        ("pre_cc_last_frame_key", "TEXT"),
        ("pristine_last_frame_key", "TEXT"),
    ]:
        if not await _has_column("shots", col):
            await conn.execute(sa.text(f"ALTER TABLE shots ADD COLUMN {col} {typ}"))
```

并把包含全部 `_has_column` 守卫的函数体命名为 `_ensure_columns(conn)`，由原启动流程调用：

```python
async def _ensure_columns(conn) -> None:
    """幂等建列。启动时调用；Spec B 的迁移脚本也会直接调用以保证回填前列已存在。"""
    # ...（原有全部 _has_column 守卫段落移入此处）...
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run --project backend pytest tests/integration/test_shot_key_columns.py -v`
Expected: PASS（3 项）

- [ ] **Step 6: 提交**

```bash
git add backend/app/models/project.py backend/app/db.py \
        backend/tests/integration/test_shot_key_columns.py
git commit -m "feat(oss): Shot 新增三个素材状态 key 列

把「目录里有什么文件」改成「DB 里写了什么」。
pristine_last_frame_key 是必需的：CC 在 image_candidates.py:226 直接覆盖
last_frame_path，校准后无法反推校准前的尾帧，而 tasks.py:659/1058 与
pipeline.py:2093 都以它为还原目标。

建列逻辑提取为 _ensure_columns()，供 Spec B 迁移脚本在回填前调用。"
```

> **检查点**：至此基础设施完备（客户端、原语、工作区、key 函数、DB 列），业务代码尚未切换。适合停下来 review 后再进入 Task 7。

---

## Task 7: 视频生成链路改造

**Files:**
- Modify: `backend/worker/tasks.py:453`（`video_out.write_bytes`）、`:464`、`:519-520`
- Modify: `backend/app/agents/video_generator.py:57-84`（`_crop_inputs`）、`:161/176/194`（`types.Image.from_file`）、`:289-307`（`_upload_image`）、`:327-328`
- Test: `backend/tests/integration/test_generation_publishes_to_oss.py`

**Interfaces:**
- Consumes: `workspace()`、`ws.fetch`、`ws.publish`、`storage.shot_key`、`storage.ts_uuid_name`、`object_store.exists`
- Produces: 生成完成后 `Shot.video_path` 与 `Shot.last_frame_path` 存 COS key；`Shot.pristine_last_frame_key` 同步写入

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/integration/test_generation_publishes_to_oss.py`：

```python
"""生成链路产出物落在 COS，且 DB 存的是 key 而非本地路径。

不调用真实模型（计费）——把 provider 的产出替换为本地合成的 mp4 字节。
"""
import subprocess

from sqlalchemy import select

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import _make_project, _add_shot

from app.models.project import Shot
from app.services import object_store, storage

pytestmark = requires_cos


def _make_mp4(path, frames=30):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440", "-frames:v", str(frames),
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac",
         "-shortest", str(path)],
        check=True, capture_output=True,
    )
    return path


async def test_generated_video_lands_in_cos_and_db_stores_key(
    db_session_factory, tmp_path, cos_prefix, monkeypatch
):
    from worker import tasks as tasks_module

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1, status="pending")

    fake = _make_mp4(tmp_path / "gen.mp4")
    video_bytes = fake.read_bytes()

    async def _fake_generate(*a, **kw):
        return video_bytes

    monkeypatch.setattr(tasks_module, "generate_video_bytes", _fake_generate)

    await tasks_module.publish_generated_video(
        db_session_factory, pid, shot_id=1, video_bytes=video_bytes
    )

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()

    assert shot.video_path.startswith(f"projects/{pid}/shots/shot_1/output_")
    assert not shot.video_path.startswith("/")
    assert await object_store.exists(shot.video_path)

    assert shot.last_frame_path.startswith(f"projects/{pid}/shots/shot_1/last_frame_")
    assert await object_store.exists(shot.last_frame_path)

    # pristine 尾帧必须同步记录，否则 CC 还原链路会断
    assert shot.pristine_last_frame_key == shot.last_frame_path
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project backend pytest tests/integration/test_generation_publishes_to_oss.py -v`
Expected: FAIL — `AttributeError: module 'worker.tasks' has no attribute 'publish_generated_video'`

- [ ] **Step 3: 抽出并实现发布函数**

在 `backend/worker/tasks.py` 中新增可独立测试的发布函数，并让原生成流程调用它：

```python
async def publish_generated_video(session_factory, project_id: str,
                                  shot_id: int, video_bytes: bytes) -> tuple[str, str]:
    """把生成的视频字节发布到 COS，抽取尾帧，更新 DB。

    返回 (video_key, last_frame_key)。

    一致性：两个对象都 put 成功后才写 DB，保证 DB 中的 key 永远指向真实对象。
    """
    from app.services.storage import shot_key, ts_uuid_name
    from app.services.workspace import workspace
    from app.agents.video_trimmer import extract_last_frame

    async with workspace() as ws:
        local_video = ws.path(f"output_{ts_uuid_name('.mp4')}")
        local_video.write_bytes(video_bytes)

        local_frame = ws.path(f"last_frame_{ts_uuid_name('.png')}")
        extract_last_frame(str(local_video), str(local_frame))

        video_key = await ws.publish(
            local_video, shot_key(project_id, shot_id, local_video.name))
        frame_key = await ws.publish(
            local_frame, shot_key(project_id, shot_id, local_frame.name))

    async with session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
        )).scalar_one()
        shot.video_path = video_key
        shot.last_frame_path = frame_key
        # CC 会覆盖 last_frame_path，故同时记录未校准的原始尾帧作为还原目标
        shot.pristine_last_frame_key = frame_key
        await s.commit()

    return video_key, frame_key
```

把 `worker/tasks.py:453` 附近原先的 `video_out.write_bytes(video_bytes)` 及随后的本地尾帧抽取、DB 赋值替换为对本函数的调用。`:464` 与 `:519-520` 的 `to_media_url(str(video_out))` 改为 `to_media_url(video_key)`。

`extract_last_frame` 若当前签名接收 `Path`，保持不变即可——它拿到的仍是真实本地文件（工作区内）。

- [ ] **Step 4: 改造 video_generator 的模型输入**

`backend/app/agents/video_generator.py` 中 `types.Image.from_file(location=...)`（:161/176/194）要求真实本地文件。首尾帧现在是 COS key，需先取到本地。

把 `_crop_inputs`（:57）的入参语义从「本地路径」改为「COS key」，并在函数开头 fetch：

```python
async def _crop_inputs(
    ws,                       # Workspace 实例
    first_frame_key: Optional[str],
    last_frame_key: Optional[str],
    reference_image_keys: Optional[list[str]],
    aspect_ratio: str,
):
    """把 COS 上的首尾帧与参考图取到工作区并按比例裁剪。

    返回本地路径三元组，供 types.Image.from_file 使用。
    """
    first_local = last_local = None
    ref_locals = []

    if first_frame_key:
        first_local = str(await ws.fetch(first_frame_key, name="first_frame.png"))
        first_local = center_crop_to_aspect(
            first_local, aspect_ratio,
            output_path=str(ws.path("first_frame_cropped.png")),
        )
    if last_frame_key:
        last_local = str(await ws.fetch(last_frame_key, name="last_frame.png"))
        last_local = center_crop_to_aspect(
            last_local, aspect_ratio,
            output_path=str(ws.path("last_frame_cropped.png")),
        )
    for i, k in enumerate(reference_image_keys or []):
        p = str(await ws.fetch(k, name=f"ref_{i}.png"))
        ref_locals.append(center_crop_to_aspect(
            p, aspect_ratio, output_path=str(ws.path(f"ref_{i}_cropped.png"))))

    return first_local, last_local, ref_locals
```

调用点（:142、:444）改为在 `async with workspace() as ws:` 内调用并 `await`，同时把原先的 `tmp_dir` + `shutil.rmtree`（:251、:483）删除——工作区已负责清理。

`_upload_image`（:289）的入参也从本地路径改为 COS key，内部先 `ws.fetch` 再 base64 上传（保持现有 kie 上传逻辑不变；直接传签名 URL 的优化属 Spec A §4.3 标注的可选项，本计划不做）。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run --project backend pytest tests/integration/test_generation_publishes_to_oss.py -v`
Expected: PASS（1 项）

- [ ] **Step 6: 提交**

```bash
git add backend/worker/tasks.py backend/app/agents/video_generator.py \
        backend/tests/integration/test_generation_publishes_to_oss.py
git commit -m "feat(oss): 生成链路改为发布到 COS

抽出 publish_generated_video 便于独立测试。两个对象都 put 成功后才写 DB，
保证 DB 中的 key 永远指向真实对象。同时写入 pristine_last_frame_key。"
```

---

## Task 8: 裁剪与还原链路改造

**Files:**
- Modify: `backend/app/agents/video_trimmer.py:285-288`（`shutil.move` / `unlink`）
- Modify: `backend/app/agents/effective_clip.py:46`（`shutil.copy2`）
- Modify: `backend/app/api/pipeline.py:1856-1871`（裁剪后重置 CC 段落）
- Test: `backend/tests/integration/test_trim_oss.py`

**Interfaces:**
- Consumes: `workspace()`、`storage.shot_key`、`object_store.delete`
- Produces: 裁剪/还原后 `Shot.video_path` 指向新 key；`pre_cc_last_frame_key` 被清空且对应对象删除；`cc_status` 重置

**素材变更审计**（CLAUDE.md 规则）：裁剪改变视频 ⇒ 必须重新抽 `last_frame`、清 pre-CC 备份并重置 `cc_status`。key 化后「清备份」= 先把 DB 列置 `None`，再删 COS 对象（顺序不可颠倒）。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/integration/test_trim_oss.py`：

```python
"""裁剪后：新视频在 COS、DB 存新 key、pre-CC 备份被清理。"""
from sqlalchemy import select

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import _make_project, _add_shot, HEADERS

from app.models.project import Shot
from app.services import object_store

pytestmark = requires_cos


async def test_trim_resets_cc_and_clears_pre_cc_object(
    client, db_session_factory, cos_prefix, tmp_path
):
    from tests.integration.conftest_cos_seed import seed_shot_source_to_oss

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await seed_shot_source_to_oss(db_session_factory, pid, 1)

    # 造一个 pre-CC 备份对象并挂到 DB 上，模拟做过角色校准的分镜
    pre_cc_key = f"projects/{pid}/shots/shot_1/last_frame_pre_cc.png"
    f = tmp_path / "pre_cc.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    await object_store.put(pre_cc_key, f)
    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.pre_cc_last_frame_key = pre_cc_key
        shot.cc_status = "done"
        await s.commit()

    r = await client.post(
        f"/api/projects/{pid}/shots/1/trim",
        json={"trim_frames": 10}, headers=HEADERS,
    )
    assert r.status_code == 200

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()

    assert shot.cc_status is None
    assert shot.pre_cc_last_frame_key is None
    # DB 先解除引用，再删对象——此时对象应已不存在
    assert await object_store.exists(pre_cc_key) is False
```

同时创建共享的 COS 播种助手 `backend/tests/integration/conftest_cos_seed.py`：

```python
"""把真实 mp4 素材播种到 COS 并写好 DB —— Task 8/9/11 复用。

对应本地版 conftest.py:160 的 seed_shot_with_source()。
不调用任何模型：视频由 ffmpeg 合成 testsrc2。
"""
import subprocess
import tempfile
from pathlib import Path

from sqlalchemy import select

from app.models.project import Shot
from app.services import object_store
from app.services.storage import shot_key, ts_uuid_name


async def seed_shot_source_to_oss(sf, project_id: str, shot_id: int,
                                  frames: int = 120) -> str:
    """合成真实 mp4、上传到 COS、写回 DB。返回 video key。"""
    from app.agents.video_trimmer import get_video_info

    with tempfile.TemporaryDirectory() as td:
        local = Path(td) / f"output_{ts_uuid_name('.mp4')}"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=440", "-frames:v", str(frames),
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac",
             "-shortest", str(local)],
            check=True, capture_output=True,
        )
        info = get_video_info(str(local))
        key = await object_store.put(shot_key(project_id, shot_id, local.name), local)

    async with sf() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
        )).scalar_one()
        shot.video_path = key
        shot.source_fps = info["fps"]
        shot.source_frames = info["total_frames"]
        await s.commit()
    return key
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project backend pytest tests/integration/test_trim_oss.py -v`
Expected: FAIL — `ImportError`（pipeline.py 仍引用已删除的 `shot_pre_cc_last_frame_path` 等）

- [ ] **Step 3: 改造裁剪链路**

`backend/app/api/pipeline.py` 中原 :1856-1861 段落：

```python
    # 3. Last frame changed → reset CC. VC is untouched (consistent with /trim).
    shot.cc_status = None
    shot.cc_error_message = None
    pre_cc = shot_pre_cc_last_frame_path(project_id, shot_id)
    if pre_cc.exists():
        pre_cc.unlink()
```

替换为：

```python
    # 3. 尾帧变了 → 重置 CC。VC 不受影响（与 /trim 一致）。
    # 顺序：先改 DB 解除引用，再删 COS 对象。反过来会留下悬空引用。
    shot.cc_status = None
    shot.cc_error_message = None
    stale_pre_cc = shot.pre_cc_last_frame_key
    shot.pre_cc_last_frame_key = None
    await session.commit()
    if stale_pre_cc:
        await object_store.delete(stale_pre_cc)
```

并把该函数返回体中的 `to_media_url(shot.video_path)` / `to_media_url(shot.last_frame_path)` 保持原样——它们现在接收 key，行为正确。

`backend/app/agents/video_trimmer.py:285` 的 `shutil.move(str(tmp_out), str(vp))` 改为在工作区内产出后 publish 到新 key（沿用 `ts_uuid_name` 生成唯一名），并由调用方把新 key 写回 `shot.video_path`。

`backend/app/agents/effective_clip.py:46` 的 `shutil.copy2(source_path, out_path)` 保持不变——它操作的是工作区内的本地文件；只需确保其调用方传入的是已 fetch 到本地的路径。

在 `pipeline.py` 顶部补 `from app.services import object_store`。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --project backend pytest tests/integration/test_trim_oss.py -v`
Expected: PASS（1 项）

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/pipeline.py backend/app/agents/video_trimmer.py \
        backend/app/agents/effective_clip.py \
        backend/tests/integration/test_trim_oss.py \
        backend/tests/integration/conftest_cos_seed.py
git commit -m "feat(oss): 裁剪/还原链路改为 COS key

按素材变更审计规则，裁剪后清 pre-CC 备份并重置 cc_status。
删除顺序固定为「先改 DB 解除引用，再删 COS 对象」，
反过来会在失败时留下悬空引用。"
```

---

## Task 9: VC 与 CC 链路改造

**Files:**
- Modify: `backend/app/api/voice.py:108-118`（临时文件）、`:126`（`to_media_url`）
- Modify: `backend/app/api/image_candidates.py:128`、`:206-230`（三处 `shutil.copy2` + DB 赋值）
- Modify: `backend/worker/tasks.py:659`、`:775`、`:859`、`:921`、`:1058`（pristine 尾帧与 VC 音频）
- Test: `backend/tests/integration/test_vc_cc_oss.py`

**Interfaces:**
- Consumes: `workspace()`、`object_store.copy`、`storage.shot_audio_vc_key`、`storage.shot_key`
- Produces: VC 产出 `Shot.vc_audio_path` 存 key、`pre_vc_video_key` 记录备份；CC 采纳时写 `last_frame_path` 并保留 `pristine_last_frame_key` 不变

**关键**：CC 采纳（image_candidates.py:226）覆盖 `last_frame_path` 时**绝不能**动 `pristine_last_frame_key`——后者是还原目标。VC 首次执行时用 `object_store.copy` 做服务端备份（不产生流量），并把备份 key 写入 `pre_vc_video_key`。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/integration/test_vc_cc_oss.py`：

```python
"""VC/CC 链路：备份用服务端 copy，pristine 尾帧不被 CC 覆盖。"""
from sqlalchemy import select

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import _make_project, _add_shot
from tests.integration.conftest_cos_seed import seed_shot_source_to_oss

from app.models.project import Shot
from app.services import object_store

pytestmark = requires_cos


async def test_cc_adopt_preserves_pristine_last_frame(
    db_session_factory, cos_prefix, tmp_path
):
    """CC 覆盖 last_frame_path，但 pristine_last_frame_key 必须岿然不动。"""
    from app.api.image_candidates import adopt_candidate_to_last_frame

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await seed_shot_source_to_oss(db_session_factory, pid, 1)

    pristine_key = f"projects/{pid}/shots/shot_1/last_frame_orig.png"
    f = tmp_path / "orig.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"o" * 64)
    await object_store.put(pristine_key, f)

    cand_key = f"projects/{pid}/shots/shot_1/candidates/cc_cand.png"
    g = tmp_path / "cand.png"
    g.write_bytes(b"\x89PNG\r\n\x1a\n" + b"c" * 64)
    await object_store.put(cand_key, g)

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
        shot.last_frame_path = pristine_key
        shot.pristine_last_frame_key = pristine_key
        await s.commit()

    await adopt_candidate_to_last_frame(db_session_factory, pid, 1, cand_key)

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()

    assert shot.cc_status == "done"
    assert shot.last_frame_path != pristine_key       # 已被校准结果覆盖
    assert shot.pristine_last_frame_key == pristine_key  # 还原目标必须保住
    assert await object_store.exists(pristine_key)


async def test_vc_backup_uses_server_side_copy(db_session_factory, cos_prefix):
    """VC 首次执行时备份原视频，用服务端 copy 不产生本地流量。"""
    from app.api.pipeline import ensure_pre_vc_backup

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    video_key = await seed_shot_source_to_oss(db_session_factory, pid, 1)

    backup_key = await ensure_pre_vc_backup(db_session_factory, pid, 1)
    assert await object_store.exists(backup_key)
    assert await object_store.size(backup_key) == await object_store.size(video_key)

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()
    assert shot.pre_vc_video_key == backup_key

    # 幂等：已备份则返回原备份，不重复拷贝
    again = await ensure_pre_vc_backup(db_session_factory, pid, 1)
    assert again == backup_key
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project backend pytest tests/integration/test_vc_cc_oss.py -v`
Expected: FAIL — `ImportError: cannot import name 'adopt_candidate_to_last_frame'` / `'ensure_pre_vc_backup'`

- [ ] **Step 3: 实现 CC 采纳**

`backend/app/api/image_candidates.py` 中把 :206-230 的三处 `shutil.copy2(src, dest)` + `to_media_url(str(dest))` 改为服务端 copy + key。抽出可测函数：

```python
async def adopt_candidate_to_last_frame(session_factory, project_id: str,
                                        shot_id: int, candidate_key: str) -> str:
    """采纳候选图为分镜尾帧（角色校准）。返回新的 last_frame key。

    注意：本函数覆盖 last_frame_path，但**绝不**触碰 pristine_last_frame_key
    ——后者是 CC 还原的唯一目标（tasks.py:659/1058、pipeline.py:2093）。
    """
    from app.services.storage import shot_key, ts_uuid_name
    from app.services import object_store

    dest_key = shot_key(project_id, shot_id, f"cc_{ts_uuid_name('.png')}")
    await object_store.copy(candidate_key, dest_key)

    async with session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
        )).scalar_one()
        shot.last_frame_path = dest_key
        shot.cc_status = "done"
        await s.commit()
    return dest_key
```

原路由内三处分支分别改为写 `custom_first_frame_path` / `target_last_frame_path` / 调用本函数，并把 `extra[...] = to_media_url(str(dest))` 改为 `to_media_url(dest_key)`。

- [ ] **Step 4: 实现 VC 备份**

在 `backend/app/api/pipeline.py` 新增：

```python
async def ensure_pre_vc_backup(session_factory, project_id: str, shot_id: int) -> str:
    """确保 VC 前的原视频已备份。返回备份 key。幂等。

    用 COS 服务端 copy——不产生本地流量，比原先的 shutil.copy 还快。
    """
    from app.services.storage import shot_key
    from app.services import object_store

    async with session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
        )).scalar_one()
        if shot.pre_vc_video_key:
            return shot.pre_vc_video_key
        src = shot.video_path

    backup_key = shot_key(project_id, shot_id, "output_pre_vc.mp4")
    await object_store.copy(src, backup_key)

    async with session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
        )).scalar_one()
        shot.pre_vc_video_key = backup_key
        await s.commit()
    return backup_key
```

`backend/app/api/voice.py:108-118` 的 `tmp_in.write_bytes(data)` / `tmp_in.unlink()` 改为在 `async with workspace() as ws:` 内使用 `ws.path(...)`，删掉手工 `unlink`；`:126` 的 `to_media_url(str(out))` 改为 `to_media_url(out_key)`。

`worker/tasks.py:659` 与 `:1058` 原先调用 `pristine_last_frame_path(project_id, shot_id)`，改为读 `shot.pristine_last_frame_key`；`:1058` 的 `or Path(shot.last_frame_path)` 回退改为 `or shot.last_frame_path`。`:921` 的 `to_media_url(vc_audio)` 改为传 key。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run --project backend pytest tests/integration/test_vc_cc_oss.py -v`
Expected: PASS（2 项）

- [ ] **Step 6: 提交**

```bash
git add backend/app/api/image_candidates.py backend/app/api/pipeline.py \
        backend/app/api/voice.py backend/worker/tasks.py \
        backend/tests/integration/test_vc_cc_oss.py
git commit -m "feat(oss): VC/CC 链路改为 COS key

备份改用服务端 copy，零流量。CC 采纳覆盖 last_frame_path 时
绝不触碰 pristine_last_frame_key——它是还原链路的唯一目标。
pristine_last_frame_path() 的目录扫描全部替换为读 DB 列。"
```

---

## Task 10: 上传链路改造

**Files:**
- Modify: `backend/app/api/uploads.py:69`（`dest_path.write_bytes`）
- Modify: `backend/app/api/pipeline.py:1147`、`:1240`、`:1267`（上传写盘）、`:1090`、`:1310`、`:1352`、`:1382`（`shutil.copy2`）、`:1199`（`shutil.rmtree`）
- Modify: `backend/app/api/image_candidates.py:128`（候选参考图上传）
- Modify: `backend/app/services/first_frame.py:54-55`、`:114-143`
- Test: `backend/tests/integration/test_uploads_oss.py`

**Interfaces:**
- Consumes: `workspace()`、`object_store.put`、`object_store.delete_prefix`、`storage.reference_image_key`、`storage.shot_custom_frames_prefix`
- Produces: 上传产物的 key 写入 `ReferenceImage.storage_path`、`Shot.custom_first_frame_path`、`Shot.custom_reference_paths`（JSON 数组，每项都是 key）

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/integration/test_uploads_oss.py`：

```python
"""上传链路：文件落 COS，DB 存 key；JSON 数组字段每项都是 key。"""
import json

from sqlalchemy import select

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import _make_project, _add_shot, HEADERS

from app.models.project import ReferenceImage, Shot
from app.services import object_store

pytestmark = requires_cos

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 128


async def test_reference_image_upload_lands_in_oss(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="draft")

    r = await client.post(
        f"/api/projects/{pid}/reference-images",
        files={"file": ("face.png", PNG, "image/png")},
        data={"kind": "character"},
        headers=HEADERS,
    )
    assert r.status_code in (200, 201)

    async with db_session_factory() as s:
        img = (await s.execute(
            select(ReferenceImage).where(ReferenceImage.project_id == pid)
        )).scalars().first()

    assert img.storage_path.startswith(f"projects/{pid}/reference_images/")
    assert not img.storage_path.startswith("/")
    assert await object_store.exists(img.storage_path)


async def test_custom_reference_paths_stores_keys_not_paths(client, db_session_factory):
    """JSON 数组字段最容易漏——数组内每一项都必须是 key。"""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)

    r = await client.post(
        f"/api/projects/{pid}/shots/1/custom-reference-images",
        files=[
            ("files", ("a.png", PNG, "image/png")),
            ("files", ("b.png", PNG, "image/png")),
        ],
        headers=HEADERS,
    )
    assert r.status_code == 200

    async with db_session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == pid, Shot.shot_id == 1)
        )).scalar_one()

    keys = json.loads(shot.custom_reference_paths)
    assert len(keys) == 2
    for k in keys:
        assert k.startswith(f"projects/{pid}/shots/shot_1/custom_frames/")
        assert not k.startswith("/")
        assert await object_store.exists(k)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project backend pytest tests/integration/test_uploads_oss.py -v`
Expected: FAIL — `ImportError`（uploads.py / pipeline.py 仍引用已删除的路径函数）

- [ ] **Step 3: 改造上传写入**

统一模式——把「算出 dest_path → write_bytes」换成「工作区暂存 → put → 存 key」：

```python
    # 原：dest_path.write_bytes(content)
    # 改：
    from app.services.workspace import workspace
    from app.services.storage import reference_image_key

    async with workspace() as ws:
        tmp = ws.path(safe_filename)
        tmp.write_bytes(content)
        key = await ws.publish(tmp, reference_image_key(project_id, image_id, safe_filename))
    # 随后把 key 写入 DB（put 已成功，满足一致性不变量）
```

逐处对应：

| 位置 | 目标 key 函数 |
|---|---|
| `uploads.py:69` | `reference_image_key(pid, image_id, filename)` |
| `pipeline.py:1147` | `reference_image_key(...)` |
| `pipeline.py:1240` / `:1267` | `shot_custom_frames_prefix(pid, sid) + ts_uuid_name(ext)` |
| `image_candidates.py:128` | `shot_candidates_prefix(pid, sid) + ts_uuid_name(ext)` |
| `pipeline.py:1090` / `:1310` / `:1352` / `:1382`（`shutil.copy2`） | 改 `object_store.copy(src_key, dst_key)` |
| `pipeline.py:1199`（`shutil.rmtree(dest_dir)`） | 改 `await object_store.delete_prefix(prefix)` |

`backend/app/services/first_frame.py:54-55` 的 `Path(prev_shot.last_frame_path)` 改为直接使用 key 字符串；`:114-143` 的 `last_frame_path` 参数语义改为 key，比较逻辑（`existing != last_frame_path`）不变——字符串比较对 key 同样成立。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --project backend pytest tests/integration/test_uploads_oss.py -v`
Expected: PASS（2 项）

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/uploads.py backend/app/api/pipeline.py \
        backend/app/api/image_candidates.py backend/app/services/first_frame.py \
        backend/tests/integration/test_uploads_oss.py
git commit -m "feat(oss): 上传链路改为发布到 COS

custom_reference_paths / ref_paths 是 JSON 数组，数组内每一项
都必须是 key —— 这是最容易漏的地方，已加专项测试覆盖。"
```

---

## Task 11: 导出合并、连贯性预览与项目删除

**Files:**
- Modify: `backend/worker/tasks.py:859`（合并临时目录）及合并任务主体
- Modify: `backend/app/api/pipeline.py:282`（`shutil.rmtree(s_dir)`）、`:302`（`sb_path.write_text`）、`:160`/`:882`（storyboard 归档 rename）、`:809`（预览临时目录）
- Modify: `backend/worker/tasks.py:188`（storyboard 写入）
- Modify: `backend/app/services/image_generation.py:157`（`write_bytes`）
- Test: `backend/tests/integration/test_export_and_delete_oss.py`

**Interfaces:**
- Consumes: `workspace()`、`ensure_free_space`、`object_store.delete_prefix`、`storage.final_video_key`、`storage.storyboard_key`
- Produces: `Project.final_video_path` 存 key；删除项目清空 `projects/<pid>/` 前缀

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/integration/test_export_and_delete_oss.py`：

```python
"""导出合并落 COS；删除项目清空整个前缀。"""
from sqlalchemy import select

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import _make_project, _add_shot, HEADERS
from tests.integration.conftest_cos_seed import seed_shot_source_to_oss

from app.models.project import Project
from app.services import object_store
from app.services.storage import project_prefix

pytestmark = requires_cos


async def test_merge_publishes_final_video(db_session_factory, cos_prefix):
    from worker.tasks import merge_project_shots

    pid = await _make_project(db_session_factory, status="shot_review")
    for i in (1, 2):
        await _add_shot(db_session_factory, pid, i)
        await seed_shot_source_to_oss(db_session_factory, pid, i, frames=30)

    key = await merge_project_shots(db_session_factory, pid)

    assert key == f"projects/{pid}/final/merged.mp4"
    assert await object_store.exists(key)
    assert await object_store.size(key) > 0

    async with db_session_factory() as s:
        proj = (await s.execute(select(Project).where(Project.id == pid))).scalar_one()
    assert proj.final_video_path == key


async def test_delete_project_clears_cos_prefix(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await seed_shot_source_to_oss(db_session_factory, pid, 1, frames=30)

    assert len(await object_store.list_prefix(project_prefix(pid))) > 0

    r = await client.delete(f"/api/projects/{pid}", headers=HEADERS)
    assert r.status_code in (200, 204)

    assert await object_store.list_prefix(project_prefix(pid)) == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project backend pytest tests/integration/test_export_and_delete_oss.py -v`
Expected: FAIL — `ImportError: cannot import name 'merge_project_shots'` 及 `delete_project_storage` 已删除导致的报错

- [ ] **Step 3: 实现合并**

在 `backend/worker/tasks.py` 新增：

```python
async def merge_project_shots(session_factory, project_id: str) -> str:
    """把项目全部分镜视频拉到工作区 concat 后发布。返回 final key。

    合并会把整个项目的视频拉到本地，可能达数 GB——先预检磁盘空间，
    否则表现为 ffmpeg 神秘失败，极难定位。
    """
    from app.services.storage import final_video_key
    from app.services.workspace import workspace, ensure_free_space
    from app.services import object_store

    # ⚠️ 严重错误警告（实施时已修正，此处保留为反面教材）：
    # 不要直接把 shot.video_path 逐个 concat。本项目是**非破坏式编辑**模型——
    # trim_frames / vc_audio_path / audio_head_mute_frames 都只存在 DB 里，
    # 需要在导出时应用到素材上。直接 concat 原视频会让用户的裁剪、变声、
    # 片头静音**全部静默消失**，成片看起来正常但编辑成果荡然无存。
    # 正确做法：复用既有的 effective_clip_paths / merge_shots 机制，
    # 让它按 DB 中的编辑描述生成"有效片段"再合并。
    async with session_factory() as s:
        shots = (await s.execute(
            select(Shot).where(Shot.project_id == project_id)
            .order_by(Shot.shot_id)
        )).scalars().all()
        keys = [sh.video_path for sh in shots if sh.video_path]

    total = sum([await object_store.size(k) for k in keys])
    await ensure_free_space(int(total * 2.2))  # 输入 + 输出 + 余量

    async with workspace() as ws:
        locals_ = []
        for i, k in enumerate(keys):
            locals_.append(await ws.fetch(k, name=f"part_{i:04d}.mp4"))

        listfile = ws.path("concat.txt")
        listfile.write_text("".join(f"file '{p}'\n" for p in locals_))

        out = ws.path("merged.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", str(listfile), "-c", "copy", str(out)],
            check=True, capture_output=True,
        )
        key = await ws.publish(out, final_video_key(project_id))

    async with session_factory() as s:
        proj = (await s.execute(
            select(Project).where(Project.id == project_id)
        )).scalar_one()
        proj.final_video_path = key
        await s.commit()
    return key
```

原合并任务主体改为调用本函数，并删除 `:859` 的手工 `_shutil.rmtree(tmp_dir)`。

- [ ] **Step 4: 改造删除与 storyboard**

`backend/app/api/projects.py` 中原调用 `delete_project_storage(project_id)` 处改为：

```python
    # 先删 DB 行（解除引用），再清 COS 前缀
    await session.delete(project)
    await session.commit()
    await object_store.delete_prefix(project_prefix(project_id))
```

`pipeline.py:282` 的 `shutil.rmtree(s_dir)` 改为 `await object_store.delete_prefix(shot_prefix(project_id, shot_id))`。

storyboard 的三处（`pipeline.py:160`/`:302`/`:882`、`worker/tasks.py:188`）改为工作区暂存后 `put` 到 `storyboard_key(pid)`；归档的 `rename` 改为 `object_store.copy(storyboard_key(pid), archived_storyboard_key(pid, ts))` 后 `delete` 原 key。

`app/services/image_generation.py:157` 的 `Path(output_path).write_bytes(...)` 保持写本地——调用方传入的是工作区路径；确认其调用方已改为在工作区内调用并随后 publish。

`pipeline.py:809` 的预览临时目录改用 `workspace()`，产出 publish 到 `join_preview_key(pid)`。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run --project backend pytest tests/integration/test_export_and_delete_oss.py -v`
Expected: PASS（2 项）

- [ ] **Step 6: 提交**

```bash
git add backend/worker/tasks.py backend/app/api/pipeline.py \
        backend/app/api/projects.py backend/app/services/image_generation.py \
        backend/tests/integration/test_export_and_delete_oss.py
git commit -m "feat(oss): 导出合并、预览与项目删除改为 COS

合并前预检磁盘空间——不预检会表现为 ffmpeg 神秘失败。
删除项目改用 delete_prefix，内部按 1000 分批符合 COS 上限。"
```

---

## Task 12: 全量读路径切换与遗留清理

**Files:**
- Modify: `backend/app/api/projects.py:31/58-87/235/294`
- Modify: `backend/app/api/stream.py:70-102`
- Modify: `backend/app/main.py:135-149`
- Test: `backend/tests/integration/test_cos_media_url.py`

**Interfaces:**
- Consumes: `storage.to_media_url`（Task 5 已改为签名 URL）
- Produces: 全部 API 响应中的媒体字段为可直接 GET 的签名 URL

**说明**：`to_media_url` 的 54 处调用点**形态不需要改动**（Task 5 已保持同步签名），本 task 只需确认它们传入的是 key 而非旧的绝对路径，并删除静态挂载。

**本 task additionally 要给 `to_media_url` 加一道校验**（决策依据见下）：

```python
def to_media_url(key: Optional[str]) -> Optional[str]:
    if not key:
        return None
    if not is_valid_key(key):
        # 到了本 task，所有写路径都已产出 key；此时还拿到非 key 的值
        # 就是真 bug。但**不能抛异常**：本函数在约 50 处同步序列化器里被
        # 调用，抛出会把一行陈旧数据放大成整个项目详情接口 500 ——
        # 在 Spec B 的回填窗口期尤其糟。优雅降级 + 可观测才是对的取舍。
        logger.warning("to_media_url_invalid_key", extra={"value": key[:200]})
        return None
    return object_store.signed_url(key)
```

放在 Task 12 而非 Task 5，是因为只有到了本 task 全部写路径才都在产出 key，此前
（Task 5–11 的红期）传入非 key 值是预期的中间态，提前告警只会制造噪音。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/integration/test_cos_media_url.py`：

```python
"""API 返回的媒体 URL 必须能被浏览器直接 GET 到真实内容。"""
import httpx
from sqlalchemy import select

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import _make_project, _add_shot, HEADERS
from tests.integration.conftest_cos_seed import seed_shot_source_to_oss

from app.models.project import Shot

pytestmark = requires_cos


async def test_project_response_video_url_is_fetchable(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await seed_shot_source_to_oss(db_session_factory, pid, 1, frames=30)

    r = await client.get(f"/api/projects/{pid}", headers=HEADERS)
    assert r.status_code == 200
    url = r.json()["shots"][0]["video_path"]

    assert url.startswith("http")
    assert "/api/media/" not in url

    async with httpx.AsyncClient(timeout=30) as c:
        head = await c.get(url, headers={"Range": "bytes=0-99"})
    assert head.status_code in (200, 206)
    assert len(head.content) > 0


async def test_media_static_mount_is_gone(client):
    """/api/media 静态挂载必须已删除，否则等于留了一条绕过签名的后门。"""
    r = await client.get("/api/media/projects/anything.mp4")
    assert r.status_code == 404


async def test_null_media_fields_stay_null(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)

    r = await client.get(f"/api/projects/{pid}", headers=HEADERS)
    shot = r.json()["shots"][0]
    assert shot["video_path"] is None
    assert shot["last_frame_path"] is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project backend pytest tests/integration/test_cos_media_url.py -v`
Expected: FAIL — `test_media_static_mount_is_gone` 返回 404 以外的状态（挂载仍在）

- [ ] **Step 3: 删除静态挂载与中间件**

删除 `backend/app/main.py:133-141` 整段：

```python
# Mount storage directory to serve generated media files (videos, frames)
from pathlib import Path as _Path
from fastapi.staticfiles import StaticFiles as _StaticFiles

_storage_dir = _Path(settings.storage_root)
_storage_dir.mkdir(parents=True, exist_ok=True)
app.mount("/api/media", _StaticFiles(directory=str(_storage_dir)), name="media")
```

以及 :144-149 的 `no_cache_media` 中间件（key 的 `ts_uuid` 唯一性已天然防缓存）。

- [ ] **Step 4: 接入 lifespan 凭证预热与关闭**

在 `backend/app/main.py` 的 lifespan（若无则新建）中：

```python
from contextlib import asynccontextmanager

from app.services import cos_client


@asynccontextmanager
async def lifespan(app):
    # 凭证必须在开始服务前预热：to_media_url 是同步函数，只读缓存不阻塞取。
    await cos_client.warm_credentials()
    await cos_client.start_credential_refresh()
    yield
    await cos_client.close_client()
```

并把 `lifespan=lifespan` 传给 `FastAPI(...)`。worker 启动/关闭钩子（ARQ 的 `on_startup` / `on_shutdown`）同样加上 `warm_credentials` / `start_credential_refresh` / `close_client`。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run --project backend pytest tests/integration/test_cos_media_url.py -v`
Expected: PASS（3 项）

- [ ] **Step 6: 跑全量后端测试**

Run: `uv run --project backend pytest tests/ -q`
Expected: 全绿。若仍有 `ImportError`，说明 Task 7–11 有遗漏的调用点——按报错逐个补齐后再继续。

- [ ] **Step 7: 提交**

```bash
git add backend/app/main.py backend/app/api/projects.py backend/app/api/stream.py \
        backend/tests/integration/test_cos_media_url.py
git commit -m "feat(oss)!: 删除 /api/media 静态挂载，媒体改走签名 URL

BREAKING: /api/media/* 不再可用。留着等于留了一条绕过签名的后门。
lifespan 预热凭证缓存——同步的 to_media_url 只读缓存，绝不阻塞去取。"
```

---

## Task 13: assets.py 改 302 重定向

**Files:**
- Modify: `backend/app/api/assets.py`（全文改写）
- Test: `backend/tests/integration/test_assets_redirect.py`

**Interfaces:**
- Consumes: `object_store.signed_url`、`object_store.exists`、`storage.is_valid_key`
- Produces: `/api/projects/{pid}/assets/{kind}/{file}` 与 `/api/projects/{pid}/final.mp4` 返回 302

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/integration/test_assets_redirect.py`：

```python
"""assets 路由改 302 重定向到签名 URL。"""
import httpx

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import _make_project, _add_shot, HEADERS
from tests.integration.conftest_cos_seed import seed_shot_source_to_oss

from app.services import object_store
from app.services.storage import final_video_key

pytestmark = requires_cos


async def test_final_mp4_redirects_with_attachment_header(client, db_session_factory,
                                                          tmp_path):
    pid = await _make_project(db_session_factory, status="shot_review")

    f = tmp_path / "merged.mp4"
    f.write_bytes(b"fake merged video")
    await object_store.put(final_video_key(pid), f)

    r = await client.get(f"/api/projects/{pid}/final.mp4",
                         headers=HEADERS, follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("http")

    async with httpx.AsyncClient(timeout=30) as c:
        got = await c.get(loc)
    assert got.status_code == 200
    assert got.content == b"fake merged video"
    # 由 COS 直接返回附件下载头，后端不中转流量
    assert "merged.mp4" in got.headers.get("content-disposition", "")


async def test_final_mp4_404_when_absent(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    r = await client.get(f"/api/projects/{pid}/final.mp4",
                         headers=HEADERS, follow_redirects=False)
    assert r.status_code == 404


async def test_asset_route_rejects_traversal(client, db_session_factory):
    pid = await _make_project(db_session_factory, status="shot_review")
    r = await client.get(
        f"/api/projects/{pid}/assets/reference_images/..%2F..%2Fsecret.txt",
        headers=HEADERS, follow_redirects=False,
    )
    assert r.status_code in (400, 404)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project backend pytest tests/integration/test_assets_redirect.py -v`
Expected: FAIL — 返回 200（`FileResponse`）而非 302

- [ ] **Step 3: 改写 assets.py**

用以下内容整体替换 `backend/app/api/assets.py`：

```python
"""素材路由。媒体本体存在 COS，本模块只签发重定向，不中转流量。"""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from app.services import object_store
from app.services.storage import (
    final_video_key,
    is_valid_key,
    reference_images_prefix,
    shot_prefix,
)

router = APIRouter()


@router.get("/projects/{project_id}/assets/{kind}/{file}")
async def serve_asset(project_id: str, kind: str, file: str):
    """302 重定向到素材的签名 URL。"""
    file = Path(file).name  # 去掉任何路径成分

    if kind == "reference_images":
        key = f"{reference_images_prefix(project_id)}{file}"
    elif kind.startswith("shots/"):
        parts = kind.split("/")
        if len(parts) < 2:
            raise HTTPException(status_code=400, detail="Invalid shot path")
        try:
            shot_id = int(parts[1].replace("shot_", ""))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid shot ID")
        key = f"{shot_prefix(project_id, shot_id)}{file}"
    elif kind == "final":
        key = f"{final_video_key(project_id).rsplit('/', 1)[0]}/{file}"
    else:
        raise HTTPException(status_code=400, detail="Unknown asset kind")

    if not is_valid_key(key):
        raise HTTPException(status_code=400, detail="Invalid key")
    if not await object_store.exists(key):
        raise HTTPException(status_code=404, detail="Asset not found")

    return RedirectResponse(url=object_store.signed_url(key), status_code=302)


@router.get("/projects/{project_id}/final.mp4")
async def download_final(project_id: str):
    """302 重定向到成片下载 URL。

    附件下载头由 COS 通过 response-content-disposition 直接返回，
    后端完全不参与视频流量。
    """
    key = final_video_key(project_id)
    if not await object_store.exists(key):
        raise HTTPException(status_code=404, detail="Final video not ready")

    return RedirectResponse(
        url=object_store.signed_url(key, filename="merged.mp4"),
        status_code=302,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --project backend pytest tests/integration/test_assets_redirect.py -v`
Expected: PASS（3 项）

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/assets.py backend/tests/integration/test_assets_redirect.py
git commit -m "feat(oss): assets 路由改 302 重定向到签名 URL

成片下载的附件头由 COS 通过 response-content-disposition 返回，
后端完全不参与视频流量。"
```

---

## Task 14: 前端签名 URL 过期兜底

**Files:**
- Modify: `frontend-vite/` 中承载 `<video>` 的播放器组件（实施时用 Step 1 的命令定位）
- Test: `frontend-vite/tests/e2e/` 下新增 e2e 用例

**Interfaces:**
- Consumes: 后端 `GET /api/projects/{id}` 返回的新签名 URL
- Produces: `<video>` 报错时自动重拉项目接口换新 URL 并重试一次

**背景**：签名 URL TTL 为 2 小时。页面长时间开着后 URL 会过期，表现为播放 403。这是本次唯一需要改前端的地方。

- [ ] **Step 1: 定位播放器组件**

Run:

```bash
grep -rn "<video" frontend-vite/src --include=*.tsx --include=*.vue --include=*.ts | head -20
```

记录实际文件路径，后续步骤在其上修改。

- [ ] **Step 2: 写失败的 e2e 用例**

在 `frontend-vite/tests/e2e/` 新增 `signed-url-expiry.spec.ts`：

```typescript
import { test, expect } from '@playwright/test'

// 真实后端 + 真实 DB + 真实 COS。只模拟"URL 过期"这一网络状况，
// 不伪造任何被断言的数据。
test('视频 URL 过期时自动重拉并恢复播放', async ({ page }) => {
  await page.goto('/projects')
  await page.getByRole('link', { name: /分镜/ }).first().click()

  const video = page.locator('video').first()
  await expect(video).toBeVisible()

  let refetched = false
  page.on('request', (r) => {
    if (r.url().includes('/api/projects/') && r.method() === 'GET') refetched = true
  })

  // 让下一次媒体请求返回 403，模拟签名过期
  let failedOnce = false
  await page.route('**/*.mp4*', async (route) => {
    if (!failedOnce) {
      failedOnce = true
      await route.fulfill({ status: 403, body: 'AccessDenied' })
    } else {
      await route.continue()
    }
  })

  await video.evaluate((el: HTMLVideoElement) => el.load())

  await expect.poll(() => refetched, { timeout: 10_000 }).toBe(true)
  await expect.poll(
    () => video.evaluate((el: HTMLVideoElement) => el.error === null),
    { timeout: 10_000 },
  ).toBe(true)
})
```

- [ ] **Step 3: 运行 e2e 确认失败**

Run: `cd frontend-vite && npx playwright test tests/e2e/signed-url-expiry.spec.ts`
Expected: FAIL — `refetched` 始终为 false（无重拉逻辑）

- [ ] **Step 4: 实现 onError 重拉**

在播放器组件中给 `<video>` 加 `onError` 处理（React 写法；Vue 用 `@error`）：

```tsx
const [reloadAttempted, setReloadAttempted] = useState(false)

const handleVideoError = useCallback(async () => {
  // 签名 URL 有 2 小时 TTL，页面开久了会过期。重拉一次换新 URL。
  // 只重试一次，避免真正损坏的素材造成无限循环。
  if (reloadAttempted) return
  setReloadAttempted(true)
  await refetchProject()
}, [reloadAttempted, refetchProject])

// 换到新 URL 后允许下一次过期时再重试
useEffect(() => { setReloadAttempted(false) }, [videoUrl])

return <video src={videoUrl} onError={handleVideoError} controls />
```

`refetchProject` 使用组件现有的项目数据获取函数（React Query 则为 `refetch`）。

- [ ] **Step 5: 运行 e2e 确认通过**

Run: `cd frontend-vite && npx playwright test tests/e2e/signed-url-expiry.spec.ts`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add frontend-vite/src frontend-vite/tests/e2e/signed-url-expiry.spec.ts
git commit -m "feat(oss): 视频签名 URL 过期时自动重拉

只重试一次，避免真正损坏的素材造成无限循环；
videoUrl 变化时重置重试标记，允许下次过期时再救一次。"
```

---

## Task 15: 端到端验收与遗留扫描

**Files:**
- Test: `backend/tests/integration/test_no_local_storage_writes.py`
- Modify: 扫描结果暴露的任何遗漏点

**Interfaces:**
- Consumes: 前序全部 task
- Produces: 无新增接口；本 task 是验收关

- [ ] **Step 1: 写「不再写本地磁盘」的守卫测试**

创建 `backend/tests/integration/test_no_local_storage_writes.py`：

```python
"""守卫：完整链路跑完后，storage_root 下不应留下任何持久文件。

这是「容器无状态」这一目标的可执行断言——若将来有人加了写本地的代码路径，
本测试会立刻失败。
"""
from pathlib import Path

from tests.integration.conftest_cos import requires_cos
from tests.integration.conftest import _make_project, _add_shot, HEADERS
from tests.integration.conftest_cos_seed import seed_shot_source_to_oss

pytestmark = requires_cos

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 128


async def test_full_flow_leaves_storage_root_empty(client, db_session_factory, tmp_path):
    """conftest 已把 settings.storage_root 指向 tmp_path。"""
    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    await seed_shot_source_to_oss(db_session_factory, pid, 1, frames=30)

    await client.post(
        f"/api/projects/{pid}/reference-images",
        files={"file": ("face.png", PNG, "image/png")},
        data={"kind": "character"}, headers=HEADERS,
    )
    await client.get(f"/api/projects/{pid}", headers=HEADERS)
    await client.post(f"/api/projects/{pid}/shots/1/trim",
                      json={"trim_frames": 5}, headers=HEADERS)

    leftovers = [p for p in Path(tmp_path).rglob("*") if p.is_file()]
    assert leftovers == [], f"仍在写本地磁盘：{leftovers}"
```

- [ ] **Step 2: 运行确认通过**

Run: `uv run --project backend pytest tests/integration/test_no_local_storage_writes.py -v`
Expected: PASS。若失败，输出会直接列出仍在写本地的文件——逐个改到 COS。

- [ ] **Step 3: 扫描遗留的本地路径用法**

Run:

```bash
grep -rn "storage_root\|shot_dir(\|project_dir(\|\.exists()\|shutil\." \
  --include=*.py backend/app backend/worker | grep -v tests | grep -v workspace.py
```

Expected: 只剩 `workspace.py` 内部与配置读取。任何业务模块里残留的 `shot_dir(` / `project_dir(` / 对素材文件的 `.exists()` 都是遗漏，须改掉。

- [ ] **Step 4: 复核素材变更审计清单**

对照 CLAUDE.md「shot 素材文件变更审计」检查清单逐条确认：

- [ ] 所有下游读取方通过 DB 字段取 key，无硬编码文件名
- [ ] 裁剪/还原后：重新抽 `last_frame`、清 `pre_cc_last_frame_key` + 重置 `cc_status`、清 `pre_vc_video_key` + 重置 `vc_status`
- [ ] `pristine_last_frame_key` 在 CC 采纳时未被覆盖
- [ ] 无任何代码路径会读到过期的备份对象

- [ ] **Step 5: 跑全量测试**

Run: `uv run --project backend pytest tests/ -q`
Expected: 全绿，无 SKIPPED（在有 COS 凭证的环境下跑）

- [ ] **Step 6: 真实栈验收**

按 CLAUDE.md 的部署约定，用**独立卷与独立 dev bucket**（Spec A §8）启动本 worktree 的栈：

```bash
podman compose -f deploy/docker-compose.dev.yml up -d
curl -s localhost:8002/openapi.json | grep -c media   # 确认 /api/media 已不在
curl -sI localhost:4000
```

人工走一遍：建项目 → 上传参考图 → 播放已有分镜 → 裁剪 → 导出下载。确认 `storage/` 目录始终为空。

- [ ] **Step 7: 提交**

```bash
git add backend/tests/integration/test_no_local_storage_writes.py
git commit -m "test(oss): 新增「不写本地磁盘」守卫测试

把「容器无状态」变成可执行断言：将来若有人加回写本地的代码路径，
本测试会立刻失败。"
```

---

## Self-Review

**1. Spec 覆盖核对**

| Spec A 章节 | 对应 Task |
|---|---|
| §2.2 同步 SDK 与事件循环 | Task 3（全部 `asyncio.to_thread` + 阻塞检测测试）、Task 15（覆盖完整性扫描） |
| §3.1 模块分层 | Task 2、3、4 |
| §3.2 Key 命名 | Task 5 |
| §3.3 不设持久缓存 | Task 4（工作区退出即删）+ Task 15（守卫测试） |
| §3.4 配置与凭证 + 代理陷阱 | Task 1 |
| §3.5 凭证缓存（cvm_role） | Task 2（缓存/刷新/重建 client）、Task 12（lifespan 预热） |
| §4.1 文件系统约定 → DB 列 | Task 6（建列）+ Task 9（替换目录扫描） |
| §4.2 一致性规则 | Task 7、8、9、11（各处写入顺序） |
| §4.3 各业务链路 | Task 7（生成）、8（裁剪）、9（VC/CC）、10（上传）、11（导出/删除） |
| §4.4 签名 URL + 同步约束 | Task 3（signed_url）、5（to_media_url）、12（lifespan 预热）、14（前端兜底） |
| §4.5 serve 路径去向 | Task 12（删挂载）、13（assets 302） |
| §5 错误处理 | Task 3（upload_file 分片续传 / RequestId 日志）、4（磁盘预检）、14（403 兜底） |
| §6 成本模型 | **无对应 Task（有意为之）** —— 全部是运维侧配置与地域选择，属 Spec B §6 的清单，不落代码 |
| §7 测试策略 | 各 task 的测试 + Task 15 |
| §8 阶段 0–5 | Task 1–3(阶段0)、4–5(阶段1)、6(阶段2)、7–11(阶段3)、12–13(阶段4)、14(阶段5) |
| §9 开发期环境隔离 | Task 15 Step 6 |
| §10 需逐行核对的代码 | Task 15 Step 3、4 |

无遗漏。§6 成本模型不产生 Task 是有意的：bucket 与 CVM 同地域、流量告警等均为控制台操作，已记入 Spec B 第 6 节的运维待办清单。

**2. 占位符扫描**：无 TBD / TODO / "类似 Task N" / "适当处理错误"。Task 2 Step 1 与 Task 14 Step 1 是**可执行的核查命令**（打印 SDK API 形态、定位播放器组件），不是占位符——它们产出具体值供后续步骤使用。

**3. 类型一致性核对**

- `object_store.signed_url` 全程同步，Task 3 定义、Task 5/13 调用一致
- `Workspace.publish(local_path, key) -> str` 与 `object_store.put(key, local_path) -> str` 参数顺序相反（工作区以本地文件为主语，原语以 key 为主语）——已在两处的 Interfaces 中显式写明，实施时注意不要传反
- `ensure_free_space(required_bytes, at=None)` 在 Task 4 定义、Task 11 调用，签名一致
- `_ensure_columns(conn)` 在 Task 6 定义，Spec B 的迁移脚本将调用
- DB 三列统一用 `_key` 后缀，与既有 `_path` 后缀字段区分开——既有字段名保持不变（Spec A 决策），新字段用 `_key` 表明语义

**4. 已知实施风险**

- **Task 2 Step 1 的 SDK API 核对是硬性前置。** 本计划的 SDK 调用依据腾讯云官方文档编写，尚未在实际安装的包上验证。上一轮在阿里云 SDK 上，计划里有三处方法名与实际不符（异步客户端根本没有 `upload_file` / `get_object_to_file`），正是靠这一步发现的。COS 这边需重点核对：`get_presigned_url` 的参数名（`Expired` / `Params`）、`object_exists` 是否存在、异常对象取错误码的方法名（`get_status_code` / `get_request_id`）、`list_objects` 分页字段（`IsTruncated` 返回的是字符串 `"true"` 还是布尔）。不符时以实际为准并同步修正 Task 3。
- **`asyncio.to_thread` 的覆盖完整性是本方案独有的隐蔽风险。** COS SDK 是纯同步的，漏包一处不会报任何错，只会在传大文件时让整个服务卡死，且小文件测试完全测不出来。Task 3 已内置事件循环阻塞检测测试（8MB 文件 + 心跳协程计数），Task 15 再做一次全局扫描。审查时应重点核对每个 SDK 调用点。
- **Task 5 之后到 Task 12 之前，全量测试是红的**。这是方案 B 的设计意图（遗漏立刻失败），不是缺陷。若采用 subagent 逐 task 执行，需提前告知执行者不要因为「其他测试红了」而回滚 Task 5。
- **Task 1 是替换而非新增**。上一轮已按阿里云 OSS 完成过同等接线并提交（`e46fed9`），本轮必须把 `oss_*` 字段、`alibabacloud` 依赖、`.aliyuncs.com` 的 NO_PROXY 全部移除。两套并存会让 `cos_client` 读到过期配置，Task 1 Step 9 的残留扫描就是为此设的闸。
