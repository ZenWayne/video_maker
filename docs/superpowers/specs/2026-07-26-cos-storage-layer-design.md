# 存储层 COS 化 — 设计文档（Spec A / 共 2 篇）

- **日期**: 2026-07-26
- **状态**: 待评审（2026-07-26 由阿里云 OSS 改为腾讯云 COS，见 §11 变更记录）
- **范围**: 把存储层从「本地路径」改造为「腾讯云 COS object key」，使代码能以 COS 为权威存储运行
- **后续**: [Spec B — 存量迁移与生产切换](./2026-07-26-cos-migration-cutover-design.md)

> **本 Spec 不可单独上生产。** 完成后代码只认 COS key，而生产 DB 中存的仍是本地绝对路径，直接部署会导致全部媒体加载失败。生产部署必须与 Spec B 一同进行。本 Spec 的验收环境是**全新的 dev 数据库 + 独立 dev bucket**。

---

## 1. 背景与目标

### 1.1 现状

媒体文件写在本地磁盘 `storage/projects/<project_id>/...`，路径逻辑集中在 `backend/app/services/storage.py`。文件通过 `main.py:135` 的 `StaticFiles` 挂载在 `/api/media` 对外提供；`to_media_url()`（storage.py:200）是绝对路径转浏览器 URL 的唯一转换点。DB 中 `Shot.video_path` 等字段存本地绝对路径。项目中不存在任何云对象存储代码。

### 1.2 目标

- 媒体资产权威存储迁至腾讯云 COS，后端/worker 容器无状态化
- 迁移到新环境只需切换 bucket，不搬运磁盘
- 现有功能行为不变：裁剪、VC、CC、导出合并、候选图等链路

### 1.3 非目标

- 不做 CDN 加速（见 §6 成本讨论，若流量费成为问题再单独评估）
- 不做浏览器直传 COS（前端上传仍走后端中转）
- 不做持久化本地缓存（见 3.3）
- 存量数据迁移、生产切换、孤儿巡检 — 均属 Spec B

---

## 2. 关键决策

| 决策项 | 结论 | 理由 |
|---|---|---|
| 云厂商 | **腾讯云 COS** | 用户的服务器在腾讯云，同地域内网访问免流量费 |
| 权威副本 | **COS 权威**，本地仅为 ffmpeg 临时工作区 | 容器彻底无状态，迁移即换 bucket |
| 访问方式 | **私有读 bucket + 后端预签名 URL** | 素材不被公网爬取 |
| 签名 URL TTL | **2 小时** | 覆盖一次编辑会话，同时收窄 URL 泄露窗口 |
| SDK | `cos-python-sdk-v5`（`qcloud_cos`） | 腾讯云官方 XML 版 Python SDK |
| **异步策略** | **SDK 为纯同步，全部 I/O 包 `asyncio.to_thread`** | COS Python SDK 没有异步客户端；直接调用会阻塞 FastAPI/ARQ 事件循环（见 2.2） |
| 开发凭证 | CAM 子账号永久密钥 `SecretId`/`SecretKey`，走现有 `/run/secrets/` | 与 `kie_api_key` 一致的注入路径 |
| 生产凭证 | **CVM 实例角色 → STS 临时密钥**（含 `Token`） | 机器上不落长期密钥 |
| 环境划分 | **全环境统一走 COS**，开发用独立 dev bucket | 代码只有一条路径，杜绝「只在生产复现」的存储 bug |
| 实现路线 | **以 COS key 为中心重写 storage.py**，本地只存在于临时工作区 | 遗漏会在所有环境立刻失败，不会潜伏到生产（见 2.1） |
| DB 字段名 | **保持 `video_path` 等名称不变**，仅语义改为 key | 免去 schema 列改名与前端联动 |
| 前端上传 | 走后端中转，不做直传 | 图片体积小，直传需配 CORS 且难以防止后端不感知的对象 |

### 2.1 为何不选「保留路径形状 + 边界插 hook」

备选方案是让 `storage.py` 继续返回 `Path`（语义改为本地缓存路径），在读写边界插 `ensure_local()` / `upload()`。改动面更小，但失败模式恶劣：**漏加一个 `ensure_local()`，在本地开发（文件恰好还在）完全正常，只在生产容器重启、本地缓存为空时才炸**。

以 key 为中心则不同——key 是字符串，当作文件路径使用必然立刻找不到文件，在所有环境同等失败。用一次性的改造成本换掉一整类潜伏到生产的 bug。

FUSE / cosfs 挂载方案已排除：容器需特权、随机写支持差（ffmpeg 输出会出问题）、性能不可控，且签名 URL 仍需单独实现。

### 2.2 同步 SDK 与事件循环

`cos-python-sdk-v5` 只提供同步的 `CosS3Client`，没有异步客户端。后端 FastAPI 与 ARQ worker 均为 async，**直接在协程里调用 SDK 会阻塞整个事件循环**——一个 200MB 的视频上传会让所有并发请求卡住。

因此 `object_store` 的每个传输方法都是 `async def`，内部用 `await asyncio.to_thread(sync_call, ...)` 把阻塞调用移出事件循环。上层（`workspace`、业务代码）看到的仍是干净的 async 接口，不感知同步实现。

SDK 另有一条硬性要求：**一个 region 只应创建一个 `CosS3Client` 实例并复用**，否则进程会占用过多连接和线程。故客户端必须是单例。

**唯一的例外是预签名**：它是纯本地 HMAC 计算，不发网络请求，因此 `signed_url()` 保持同步、直接调用 SDK，不进线程池（见 4.4）。

---

## 3. 架构

### 3.1 模块分层

```
app/services/
  cos_client.py     ← 客户端与凭证（唯一 import qcloud_cos 的模块）
  object_store.py   ← 对象操作原语（put/get/exists/copy/delete/delete_prefix/signed_url/list_prefix）
  workspace.py      ← ffmpeg 临时工作区（fetch/path/publish）
  storage.py        ← 改造：路径函数 → key 函数
```

**`cos_client.py`** — 全项目唯一 import `qcloud_cos` 的模块。持有 `CosS3Client` 单例。凭证按 `cos_auth_mode` 分发：

- `static`（开发）：`CosConfig(Region=, SecretId=, SecretKey=, Scheme=)`
- `cvm_role`（生产）：从实例元数据服务取 STS 临时密钥
  `http://metadata.tencentyun.com/latest/meta-data/cam/security-credentials/<角色名>`
  返回 `TmpSecretId` / `TmpSecretKey` / `Token` / `ExpiredTime`，构造 `CosConfig(..., Token=...)`。
  **临时密钥会过期，因此必须缓存并周期刷新**（见 3.5）。

**`object_store.py`** — 不含业务语义的对象原语。全部为 `async def`，内部 `asyncio.to_thread` 包裹同步 SDK 调用。`copy` 使用 COS 服务端拷贝，pre-VC / pre-CC 备份因此不产生流量。

**`workspace.py`** — ffmpeg 临时工作区（与云厂商无关，不受本次变更影响）：

```python
async with workspace() as ws:
    src = await ws.fetch(shot_video_key(pid, sid))    # COS → tmpdir
    out = ws.path("last_frame.png")                    # tmpdir 内新文件
    await extract_last_frame(src, out)
    await ws.publish(out, shot_last_frame_key(pid, sid))
# 退出即删除 tmpdir
```

### 3.2 Key 命名

key 完全沿用现有 storage_root 相对路径布局，一一对应：

```
projects/<project_id>/shots/shot_<n>/output_<ts>_<uuid>.mp4
projects/<project_id>/shots/shot_<n>/last_frame_<ts>_<uuid>.png
projects/<project_id>/final/merged.mp4
```

迁移脚本因此是「本地相对路径 = key」的直接映射。`ts_uuid_name()`（storage.py:13）保留。DB 存**裸 key**，不加任何 scheme 前缀、不带前导 `/`。

### 3.3 不设持久本地缓存

浏览器通过签名 URL 直连 COS，后端不参与媒体传输；本地文件只有 ffmpeg 需要，而 ffmpeg 任务都是离散的。因此用完即弃的临时工作区已足够，不需要 LRU 缓存与淘汰逻辑。

导出合并需拉取全部分镜视频这一处属性能优化而非正确性需求，按 YAGNI 不提前设计。

### 3.4 配置与凭证

`config.py` 字段：

| 字段 | 归属 | 说明 |
|---|---|---|
| `cos_region` | `deploy/config.yml` | 如 `ap-guangzhou`。**应与 CVM 同地域**，后端↔COS 才走内网免流量费 |
| `cos_bucket` | `deploy/config.yml` | **必须带 AppId**，形如 `video-maker-dev-1250000000` |
| `cos_scheme` | `deploy/config.yml` | 默认 `https` |
| `cos_domain` | `deploy/config.yml` | 自定义源站域名，留空则用默认域名 |
| `cos_auth_mode` | `deploy/config.yml` | `static` \| `cvm_role` |
| `cos_cvm_role` | `deploy/config.yml` | `cvm_role` 模式下的角色名 |
| `cos_signed_url_ttl_sec` | `deploy/config.yml` | 默认 `7200` |
| `cos_secret_id` | `secrets.yml` | 仅 static 模式 |
| `cos_secret_key` | `secrets.yml` | 仅 static 模式 |

密钥复用现有链路：`secrets.yml` → `make secrets` → `deploy/secrets/<key>` → compose `secrets:`（路径写 `./secrets/...`，与既有 6 条一致）→ `/run/secrets/` → 容器 command 中 `export`。

依赖：`backend/pyproject.toml` 加 `cos-python-sdk-v5`。**不需要 aiohttp**（SDK 基于 requests）。CVM 角色凭证走元数据服务的普通 HTTP GET，用已有的 `httpx` 即可，**不需要额外的腾讯云凭证 SDK**。

**代理陷阱（必须处理）**：`docker-compose.dev.yml` 为 Google API 设置了 `HTTPS_PROXY`。requests 默认读取环境代理，COS 作为境内服务走该代理既慢又可能失败。必须设置 `NO_PROXY` / `no_proxy` 包含 **`.myqcloud.com`**（COS 默认域名是 `{bucket-appid}.cos.{region}.myqcloud.com`）。漏掉的表现是上传随机超时，极难定位。

### 3.5 凭证缓存（`cvm_role` 模式）

`to_media_url()` 必须保持同步（见 4.4），而签名需要凭证。`cvm_role` 模式下取凭证是网络操作，不能在同步函数里做。方案：

- 模块内维护凭证缓存（`TmpSecretId` / `TmpSecretKey` / `Token` / 过期时间）
- FastAPI lifespan 与 worker 启动时**预热一次**；后台协程按凭证剩余有效期的 50% 周期刷新
- `signed_url()` 只读缓存，绝不触发网络请求；缓存为空时抛异常而非阻塞（由预热保证不发生）
- **签名有效期取 `min(cos_signed_url_ttl_sec, 凭证剩余有效期)`** —— 签名有效期不能超过临时密钥有效期，否则 URL 会在 2 小时 TTL 内因凭证先过期而失效
- 临时密钥变化时需**重建 `CosS3Client`**（`Token` 是构造 `CosConfig` 时传入的），这是与永久密钥模式的一个关键差异
- `static` 模式下密钥是常量，以上机制全部退化为直接返回，无额外开销

---

## 4. 数据流改造

### 4.1 文件系统约定 → 显式 DB 字段

现有代码多处从文件系统推导状态，在对象存储下均不成立：

| 位置 | 现状 | 问题 |
|---|---|---|
| `pristine_video_path()` storage.py:77 | `glob("output_*.mp4")` 取 mtime 最新 | 本地目录为空 → 返回 `None`，进而 `get_original_video_for_audio()`（:112）抛 `FileNotFoundError`，VC 链路断裂 |
| `pristine_last_frame_path()` storage.py:89 | `glob("last_frame_*.png")` 排除固定名备份 | 同上 |
| `shot_pre_vc_video_path()` storage.py:62 | 依赖固定名 `output_pre_vc.mp4` 是否存在 | 存在性判断变成一次 COS 请求 |
| `shot_pre_cc_last_frame_path()` storage.py:67 | 依赖固定名 `last_frame_pre_cc.png` | 同上 |

一律改为显式 DB 列（新增三列）。

> **迁移机制**：本项目**不使用 alembic**（`backend/pyproject.toml` 无该依赖，仓库中也无 `alembic/` 目录）。schema 变更一律写在 `app/db.py`，由 `_has_column()` 守卫的幂等 `ALTER TABLE` 完成（既有写法见 db.py:55-115）。

- `Shot.pre_vc_video_key` — 替代「`output_pre_vc.mp4` 是否存在」
- `Shot.pre_cc_last_frame_key` — 替代「`last_frame_pre_cc.png` 是否存在」（现由 pipeline.py:1860 的 `pre_cc.exists()` 判断）
- `Shot.pristine_last_frame_key` — 替代 `pristine_last_frame_path()` 的目录扫描
- pristine 视频：非破坏性模型下即 `Shot.video_path` 本身，无需新列

**关于 pristine 尾帧为何必须独立成列**（已核实）：角色校准在 image_candidates.py:226-227 直接覆盖 `shot.last_frame_path` 并置 `cc_status="done"`，因此 CC 之后无法从任何现有字段反推出校准前的尾帧。而 `pristine_last_frame_path()` 正是 worker/tasks.py:659、worker/tasks.py:1058 与 pipeline.py:2093 的还原目标——目录扫描一旦失效，CC 还原链路即断裂。

> **与 Spec B 的衔接**：这三列的**初值只能在本地文件尚存时扫描推导**，因此其回填由 Spec B 的迁移脚本负责，且是一次性、不可补做的。

该改动同时正面满足 CLAUDE.md 中「shot 素材文件变更审计」规则：素材权威状态从「目录里有什么文件」变为「DB 里写了什么」。

### 4.2 一致性规则

COS 与 DB 是两个系统，无分布式事务。固定写入顺序：

- **新增**：先 `put` 到 COS，成功后写 DB
- **删除**：先改 DB 解除引用，成功后删 COS
- **替换**：新 key 先传 → DB 指过去 → 删旧 key（`ts_uuid_name` 保证不同名）

**不变量：DB 中的 key 永远指向真实存在的对象。** 任何中途失败只产生无人引用的孤儿对象。反向做法会让用户看到不可自愈的播放 403。

### 4.3 各业务链路

**ffmpeg 类**（抽帧 / 裁剪 / VC 提音轨 / CC / 导出合并 / 连贯性预览）统一套 `workspace()`：fetch 输入 → 本地计算 → publish 输出 → 更新 DB。

导出合并需将整个项目全部分镜视频拉入 tmpdir 再 concat，20 个分镜可能达数 GB。需在 fetch 前预检磁盘空间。

**视频生成**（`agents/video_generator.py`）：Vertex 的 `types.Image.from_file()`（video_generator.py:161/176/194）需真实本地文件，首尾帧须先 `ws.fetch()`。

kie provider 的 `_upload_image()`（video_generator.py:289）现将本地帧 base64 上传至 kie CDN；素材上 COS 后可直接传签名 URL 给 kie。**列为可选优化，不与主改造绑定**。

**前端上传**：`uploads.py` 收到 multipart 后写临时文件 → publish → DB 存 key。

**删除项目**：`delete_project_storage()`（storage.py:184）的 `shutil.rmtree` 换成 `delete_prefix("projects/<pid>/")`。COS 批量删除单次上限 1000 个 key，需分页循环。

### 4.4 签名 URL 与前端时效性

`to_media_url(key)` 改为返回预签名 URL（COS SDK 的 `get_presigned_url` / `get_presigned_download_url`）。

**`to_media_url()` 必须保持同步**：`_shot_to_dict()`（projects.py:38）与 `_candidate_to_dict()`（projects.py:22）是**同步**函数，且在同步列表推导中调用它。改成 `async` 会连锁污染全部序列化器及其上游调用者。预签名是纯本地 HMAC 计算、不发网络请求，因此同步调用不阻塞事件循环——这也是它不进 `asyncio.to_thread` 的原因。

- TTL 取 **2 小时**，`cvm_role` 模式下再与凭证剩余有效期取小（见 3.5）
- 每次 API 响应现签一次，成本约等于零，无需缓存或复用 URL
- SSE 推送的 URL 同样现签，不存在推送陈旧 URL 的问题
- COS 原生支持 Range 请求，签名 URL 亦然，进度条拖动无需额外处理
- **过期兜底**：前端 `<video>` 的 `onError` 触发时重新拉取项目接口换取新 URL。这是本次唯一需要改动前端的地方

### 4.5 现有 serve 路径的去向

| 现状 | 处理 |
|---|---|
| `main.py:135` `/api/media` StaticFiles 挂载 | 删除 |
| `main.py:144` `/api/media/*` 强制 no-cache 中间件 | 删除（key 唯一性已天然防缓存） |
| `assets.py:17` `serve_asset` | 保留路由，改为 302 重定向到签名 URL |
| `assets.py:48` `download_final` | 302 重定向，签名中带 `response-content-disposition`，由 COS 直接返回附件下载头 |
| `storage.py:212` `validate_safe_path` | 换成 key 校验：禁 `..`、必须以 `projects/` 开头 |

附带效果：`validate_safe_path` 现用 `str.startswith` 判断路径包含关系（storage.py:223），`/storage-evil` 会被误判为位于 `/storage` 内。换成 key 校验后该问题自然消失。

---

## 5. 错误处理

| 场景 | 处理 |
|---|---|
| **上传失败** | **用 SDK 的 `upload_file()` 高级接口**——它按文件大小自动选择简单/分块上传，分块上传自带断点续传。参数 `PartSize`、`MAXThread` 可调。比手写分片可靠得多 |
| **下载失败** | `ws.fetch()` 失败即让任务失败并标记 shot 状态，**绝不静默降级**。明确失败可重试，优于悄悄使用错误文件 |
| **凭证失效** | `cvm_role` 自动刷新；static 密钥失效表现为全量 403。日志须区分「403 凭证/权限问题」与「404 对象不存在」，并记录 COS 返回的 **RequestId** 与错误码（腾讯云工单排查以 RequestId 为准） |
| **tmpdir 空间不足** | 导出合并前检查可用空间是否足以容纳全部分镜，不足则明确报错。否则表现为 ffmpeg 神秘失败 |
| **签名 URL 403** | 前端 `onError` 重新拉取接口换取新 URL（见 4.4） |
| **事件循环阻塞** | 所有传输操作必须经 `asyncio.to_thread`。**遗漏一处的表现是整个服务在大文件传输期间卡死**，且只在文件够大时才复现——审查时需重点核对 |

**日志**沿用项目现有 `python-json-logger`，每次对象操作记录 key、操作类型、耗时、字节数、RequestId。

---

## 6. 成本模型（腾讯云 COS）

免费额度（个人用户）：**50GB 标准存储，6 个月（180 天）**。但**不覆盖**请求数、数据取回、以及最关键的**流量**。

| 计费项 | 是否在免费额度内 | 对本项目的影响 |
|---|---|---|
| 标准存储容量 | ✅ 50GB / 6 个月 | 素材存量，短期内基本免费 |
| **外网下行流量** | ❌ 约 0.5 元/GB | **主要成本**。浏览器经签名 URL 直连 COS 拉视频，每次预览/播放都计入 |
| 请求次数 | ❌ 约 0.01 元/万次 | 量级极小，可忽略 |
| 内网流量（同地域） | 免费 | 后端/worker ↔ COS 的 fetch/publish |

**结论与取舍**：

- **bucket 必须与 CVM 同地域**，否则后端每次 ffmpeg 取素材都走外网计费。这是 §3.4 里 `cos_region` 那条约束的真正原因，也是本次从阿里云换到腾讯云的核心动机。
- 浏览器侧的外网下行无法避免——即便改回「后端代理转发」方案，流量总量相同，只是换了出口。所以签名 URL 方案在成本上没有劣势。
- 若日后流量费显著，可评估接 CDN（CDN 回源流量另计，但分发侧单价低于 COS 直出）。**本次不做**。
- 这一成本结构也影响 Spec B 的取舍：孤儿对象涨的是**存储费**（有 50GB 免费额度兜底），而播放涨的是**流量费**（无免费额度）。因此「孤儿只做 dry-run 巡检」的风险敞口比在阿里云方案下更小。

---

## 7. 测试策略

项目规则明确：除会计费的模型调用外一律不 mock。COS 是真实基础设施而非计费模型边界，**因此测试必须打真实 COS**，不实现任何 fake object store。

**隔离方式**：dev bucket + 每次测试运行使用唯一前缀 `test/<run_id>/`，teardown 删除整个前缀。测试文件为 KB~MB 级，流量与存储费均可忽略。

| 层次 | 做法 |
|---|---|
| **key 函数单元测试** | 纯函数，直接断言 key 拼接，无需网络 |
| **object_store / workspace** | 打真实 dev bucket：put/get/copy/delete/list/签名 |
| **签名 URL** | **断言真能 `GET` 到内容**，而非断言 URL 字符串形态——签名算错时字符串形态依然正确 |
| **不阻塞事件循环** | 针对 `object_store` 的传输方法，断言其确实在线程池中执行（上传较大文件期间，另一个协程仍能及时推进） |
| **ffmpeg 链路集成测试** | 复用已有真实 `output_<ts>_<uuid>.mp4` 素材，走完整 fetch → ffmpeg → publish → DB，**不调用任何模型** |
| **e2e (Playwright)** | 真实后端 + 真实 DB + 真实 COS，仅短路项目规则中列明的 AI 触发端点 |

后端测试用 `uv run pytest` 直接运行，不套 podman。需要 COS 凭证的测试打 marker。

---

## 8. 分阶段落地

| 阶段 | 内容 |
|---|---|
| **0** | 依赖、config 字段、secrets 接线、`cos_client`、`object_store` |
| **1** | `workspace()` 原语 + `storage.py` key 函数 |
| **2** | 在 `app/db.py` 按 `_has_column()` 幂等模式新增三列 |
| **3** | **写路径**改造：生成、上传、各 ffmpeg 链路，产出并存 key |
| **4** | **读路径**改造：`to_media_url` 签名、`assets.py` 改 302、删 `/api/media` 挂载 |
| **5** | 前端 `onError` 重拉换新 URL；删除 `storage.py` 遗留的本地路径函数 |

阶段 3 必须先于阶段 4：读路径能工作的前提是写路径已在产出 key。

**验收标准**：在全新 dev DB + 独立 dev bucket 上，从建项目到导出成片的完整链路跑通，且本地 `storage/` 目录中除临时工作区外不产生任何持久文件。

---

## 9. 开发期环境隔离

按项目部署约定，**所有 worktree 共用同一套 `deploy_app-data`（DB）与 `deploy_app-storage`（媒体）卷**，同一时刻仅一个栈运行。

本 Spec 的改造会让代码只认 key，若跑在共享 DB 上会读到旧的绝对路径而全面失败；反之若在共享 DB 上执行 Spec B 的回填，再切回任何旧 worktree 也会全面失败。

**已采纳的处理方式**：开发期间为本 worktree 使用**独立的 DB 卷与独立 dev bucket**，完全不触碰共享卷。

---

## 10. 实施阶段需要逐行核对的代码

1. **全部素材读写点的清单** — 需完整枚举 `worker/tasks.py`、`app/api/pipeline.py`、`app/api/image_candidates.py`、`app/api/voice.py`、`app/api/uploads.py`、`app/agents/` 中所有写入或读取素材文件的位置。遗漏一处即为一条运行时故障。
2. **CLAUDE.md「shot 素材文件变更审计」清单的逐条复核** — 裁剪 / 还原 / VC / CC 各自需要清理的关联文件与需重置的 status 字段。
3. **`_reset_tail_frame()`（pipeline.py:44）等「路径即真相」的辅助函数** — 注释写着 "Path-as-truth: a tail frame is used iff target_last_frame_path is set"。语义在 key 化后仍成立，但需确认同类模式没有别处依赖文件真实存在性。
4. **`asyncio.to_thread` 的覆盖完整性** — 每一个 SDK 调用都必须在线程池中执行（签名除外）。这是 COS 方案独有的风险，遗漏不会报错，只会在大文件传输时让服务卡死。

---

## 11. 变更记录

**2026-07-26：由阿里云 OSS 改为腾讯云 COS。** 原因：用户的服务器在腾讯云，同地域内网访问免流量费。

架构层面**未变**的部分——这正是选择方案 B 的附带收益，云厂商被隔离在一个模块内：
key 命名与布局、`workspace` 原语、`storage.py` 的 key 化、三个新增 DB 列、一致性规则、
读写路径改造范围、签名 URL 策略与同步约束、测试策略、分阶段划分。

**变更的部分**：

| 项 | 阿里云 OSS | 腾讯云 COS |
|---|---|---|
| SDK | `alibabacloud-oss-v2` + aiohttp | `cos-python-sdk-v5`（`qcloud_cos`） |
| 异步支持 | 原生 `AsyncClient` | **无异步客户端，全部包 `asyncio.to_thread`** |
| 客户端模块 | `oss_client.py` | `cos_client.py` |
| 凭证字段 | AccessKeyId / AccessKeySecret | **SecretId / SecretKey** |
| 生产免密 | ECS RAM Role | **CVM 实例角色**（元数据服务取 STS 临时密钥） |
| bucket 命名 | 全局唯一名称 | **必须带 AppId**：`name-1250000000` |
| 大文件上传 | AsyncClient 无 `upload_file`，需 `to_thread` 跑同步 Uploader | **SDK 原生 `upload_file()`**，自动分块 + 断点续传 |
| 下载到文件 | AsyncClient 无 `get_object_to_file`，需手动流式写盘 | **`response['Body'].get_stream_to_file()`** |
| NO_PROXY 域名 | `.aliyuncs.com` | **`.myqcloud.com`** |
| 免费额度 | — | 50GB 标准存储 / 6 个月，**不含流量**（见 §6） |

**已作废的阿里云特定结论**：为规避 `AsyncClient` 缺失 `upload_file` / `get_object_to_file` 而设计的两处变通（`asyncio.to_thread` 跑同步 `oss.Uploader`、`AsyncStreamBody` 手动流式写盘），在 COS 下均不需要——SDK 原生提供了对应高级接口。反过来，COS 引入了一个阿里云方案没有的新风险：**同步 SDK 的事件循环阻塞**（见 2.2 与 §5 末行）。
