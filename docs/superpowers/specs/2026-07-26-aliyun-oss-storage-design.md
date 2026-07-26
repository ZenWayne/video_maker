# 阿里云 OSS 持久化存储接入 — 设计文档

- **日期**: 2026-07-26
- **状态**: 待评审
- **目标**: 把 shot 媒体资产（视频、音频、帧图）的权威存储从本地磁盘迁移到阿里云 OSS，使后端/worker 容器无状态化，满足正式上线的云原生与可迁移性要求。

---

## 1. 背景与目标

### 1.1 现状

所有媒体文件写在本地磁盘 `storage/projects/<project_id>/...`，路径逻辑集中在 `backend/app/services/storage.py`。文件通过 `main.py:135` 的 `StaticFiles` 挂载在 `/api/media` 对外提供，`to_media_url()`（storage.py:200）是绝对路径转浏览器 URL 的唯一转换点。DB 中 `Shot.video_path` 等字段存的是本地绝对路径。项目中不存在任何云对象存储代码。

### 1.2 目标

- 媒体资产持久化到 OSS，容器可无状态重启、可换机、可多副本
- 迁移到新环境只需切换 bucket，不搬运磁盘
- 不牺牲现有功能：裁剪、VC、CC、导出合并、候选图等链路行为保持不变

### 1.3 非目标

- 不做 CDN 加速与公开分享链接（bucket 保持私有读）
- 不做浏览器直传 OSS（前端上传仍走后端中转）
- 不做持久化本地缓存（见 3.3）
- 不做孤儿对象的自动删除（本次仅产出 dry-run 报告脚本）

---

## 2. 关键决策

| 决策项 | 结论 | 理由 |
|---|---|---|
| 权威副本 | **OSS 权威**，本地仅为 ffmpeg 临时工作区 | 容器彻底无状态，迁移即换 bucket |
| 访问方式 | **私有 bucket + 后端预签名 URL** | 素材不被公网爬取；签名是本地 HMAC 计算，零网络成本 |
| 签名 URL TTL | **2 小时** | 覆盖一次编辑会话，同时收窄 URL 泄露窗口 |
| 生产凭证 | **ECS RAM Role**（自动刷新 STS） | 无需维护 AK/SK；配 `use_internal_endpoint` 免公网流量费 |
| 开发凭证 | **RAM 用户 AK/SK**，走现有 `/run/secrets/` 机制 | 与 `kie_api_key` 完全一致的注入路径 |
| 环境划分 | **全环境统一走 OSS**，开发用独立 dev bucket | 代码只有一条路径，杜绝「只在生产复现」的存储 bug |
| 存量数据 | **一次性迁移脚本全量上云** + DB 回填 | 上线后存储层只有一条路径，无需长期兼容两种格式 |
| 实现路线 | **以 OSS key 为中心重写 storage.py**，本地只存在于临时工作区 | 漏写会在所有环境立刻失败，不会潜伏到生产（见 2.1） |
| DB 字段名 | **保持 `video_path` 等名称不变**，仅语义改为 key | 免去 alembic 列改名与前端联动；响应体给前端的本来就是 URL |
| 前端上传 | 走后端中转，不做直传 | 图片体积小，直传需配 CORS 且难以防止后端不感知的对象 |
| 孤儿 GC | **本次仅 dry-run** | 唯一有不可逆破坏风险的组件，先观察实际孤儿量 |
| 开发期数据卷 | **独立 DB 卷 + 独立 bucket**，不碰共享卷 | 规避 4.3 的 worktree 协作风险 |

### 2.1 为何不选「保留路径形状 + 边界插 hook」

备选方案是让 `storage.py` 继续返回 `Path`（语义改为本地缓存路径），在读写边界插 `ensure_local()` / `upload()`。改动面更小，但失败模式恶劣：**漏加一个 `ensure_local()`，在本地开发（文件恰好还在）完全正常，只在生产容器重启、本地缓存为空时才炸**。

以 key 为中心则不同——key 是字符串，当作文件路径使用必然立刻找不到文件，在所有环境同等失败。用一次性的改造成本换掉一整类潜伏到生产的 bug。

FUSE / ossfs 挂载方案已排除：容器需特权、随机写支持差（ffmpeg 输出会出问题）、性能不可控，且签名 URL 仍需单独实现。

---

## 3. 架构

### 3.1 模块分层

```
app/services/
  oss_client.py     ← 客户端与凭证（唯一 import alibabacloud_oss_v2 的模块）
  object_store.py   ← 对象操作原语（put/get/exists/copy/delete/delete_prefix/signed_url/list_prefix）
  workspace.py      ← ffmpeg 临时工作区（fetch/path/publish）
  storage.py        ← 改造：路径函数 → key 函数
```

**`oss_client.py`** — 懒加载单例 `AsyncClient`。必须使用 `alibabacloud_oss_v2.aio.AsyncClient`（要求 SDK ≥ 1.2.0 + aiohttp），因为 FastAPI 与 ARQ worker 均为 async，同步 client 会阻塞事件循环。凭证按 `oss_auth_mode` 分发：

- `ecs_ram_role`：`alibabacloud_credentials` 的 `Config(type='ecs_ram_role', role_name=...)`，包装成 `oss.credentials.CredentialsProviderFunc`
- `static`：`oss.credentials.StaticCredentialsProvider`，AK/SK 由 `/run/secrets/` 注入环境变量

在 FastAPI lifespan 与 worker shutdown 中 `await client.close()`，否则连接泄漏。

**`object_store.py`** — 不含业务语义的对象原语。`copy` 使用 OSS 服务端拷贝，pre-VC / pre-CC 备份因此不产生任何流量（现状是本地 `shutil.copy`）。

**`workspace.py`** — 方案 B 的核心原语：

```python
async with workspace() as ws:
    src = await ws.fetch(shot_video_key(pid, sid))    # OSS → tmpdir
    out = ws.path("last_frame.png")                    # tmpdir 内新文件
    await extract_last_frame(src, out)
    await ws.publish(out, shot_last_frame_key(pid, sid))
# 退出即删除 tmpdir
```

退出时无条件清理。

### 3.2 Key 命名

key 完全沿用现有 storage_root 相对路径布局，一一对应：

```
projects/<project_id>/shots/shot_<n>/output_<ts>_<uuid>.mp4
projects/<project_id>/shots/shot_<n>/last_frame_<ts>_<uuid>.png
projects/<project_id>/final/merged.mp4
```

迁移脚本因此是「本地相对路径 = key」的直接映射，零转换逻辑；也便于人工在 OSS 控制台按项目定位。

`ts_uuid_name()`（storage.py:13）保留，作用从「防浏览器缓存」变为「保证 key 唯一 + 防缓存」。

DB 存**裸 key**，不加 `oss://` 前缀。

### 3.3 不设持久本地缓存

浏览器通过签名 URL 直连 OSS，后端不参与媒体传输；本地文件只有 ffmpeg 需要，而 ffmpeg 任务都是离散的。因此用完即弃的临时工作区已足够，不需要 LRU 缓存与淘汰逻辑。

唯一的性能例外是导出合并需拉取全部分镜视频。这属于性能优化而非正确性需求，按 YAGNI 不提前设计；若日后确实偏慢，再单独为该链路加缓存。

### 3.4 配置与凭证

`config.py` 新增字段：

| 字段 | 归属 | 说明 |
|---|---|---|
| `oss_region` | `deploy/config.yml` | 如 `cn-hangzhou` |
| `oss_bucket` | `deploy/config.yml` | dev / prod 不同 bucket |
| `oss_use_internal_endpoint` | `deploy/config.yml` | 生产 ECS 同地域置 true |
| `oss_auth_mode` | `deploy/config.yml` | `static` \| `ecs_ram_role` |
| `oss_ecs_ram_role` | `deploy/config.yml` | 生产填角色名以减少元数据请求 |
| `oss_signed_url_ttl_sec` | `deploy/config.yml` | 默认 7200 |
| `oss_access_key_id` | `secrets.yml` | 仅 static 模式 |
| `oss_access_key_secret` | `secrets.yml` | 仅 static 模式 |

密钥复用现有链路：`secrets.yml` → `make secrets` → `deploy/secrets/<key>` → compose `secrets:` → `/run/secrets/` → 容器 command 中 `export`。同时需补充 `deploy/secrets.yml.example`。

新依赖加入 `backend/pyproject.toml`：`alibabacloud-oss-v2>=1.2.0`、`aiohttp`、`alibabacloud_credentials`。

**代理陷阱（必须处理）**：`docker-compose.dev.yml` 为 Google API 设置了 `HTTPS_PROXY: http://host.containers.internal:10809`。aiohttp 默认读取环境代理，OSS 作为境内服务走该代理既慢又可能失败。必须设置 `NO_PROXY` 包含 `.aliyuncs.com`，并在客户端构造时验证代理未生效。漏掉的表现是上传随机超时，极难定位。

---

## 4. 数据流改造

### 4.1 文件系统约定 → 显式 DB 字段

现有代码多处从文件系统推导状态，在对象存储下均不成立：

| 位置 | 现状 | 问题 |
|---|---|---|
| `pristine_video_path()` storage.py:77 | `glob("output_*.mp4")` 取 mtime 最新 | 本地目录为空 → 返回 `None`，进而 `get_original_video_for_audio()`（:112）抛 `FileNotFoundError`，VC 链路断裂 |
| `pristine_last_frame_path()` storage.py:89 | `glob("last_frame_*.png")` 排除固定名备份 | 同上 |
| `shot_pre_vc_video_path()` storage.py:62 | 依赖固定名 `output_pre_vc.mp4` 是否存在 | 存在性判断变成一次 OSS 请求 |
| `shot_pre_cc_last_frame_path()` storage.py:67 | 依赖固定名 `last_frame_pre_cc.png` | 同上 |

一律改为显式 DB 列：

- `Shot.pre_vc_video_key` — 替代「`output_pre_vc.mp4` 是否存在」（现由 `pipeline.py` 的 `.exists()` 判断）
- `Shot.pre_cc_last_frame_key` — 替代「`last_frame_pre_cc.png` 是否存在」（现由 pipeline.py:1860 的 `pre_cc.exists()` 判断）
- `Shot.pristine_last_frame_key` — 替代 `pristine_last_frame_path()` 的目录扫描
- pristine 视频：非破坏性模型下即 `Shot.video_path` 本身，无需新列

**关于 pristine 尾帧为何必须独立成列**（已核实）：角色校准在 image_candidates.py:226-227 直接覆盖 `shot.last_frame_path` 并置 `cc_status="done"`，因此 CC 之后无法从任何现有字段反推出校准前的尾帧。而 `pristine_last_frame_path()` 正是 worker/tasks.py:659、worker/tasks.py:1058 与 pipeline.py:2093 的还原目标——目录扫描一旦失效，CC 还原链路即断裂。故必须新增 `pristine_last_frame_key` 列。

该改动同时正面满足 CLAUDE.md 中「shot 素材文件变更审计」规则：素材权威状态从「目录里有什么文件」变为「DB 里写了什么」，审计成为可静态检查的事情。

### 4.2 一致性规则

OSS 与 DB 是两个系统，无分布式事务。固定写入顺序：

- **新增**：先 `put` 到 OSS，成功后写 DB
- **删除**：先改 DB 解除引用，成功后删 OSS
- **替换**：新 key 先传 → DB 指过去 → 删旧 key（`ts_uuid_name` 保证不同名）

**不变量：DB 中的 key 永远指向真实存在的对象。** 任何中途失败只产生无人引用的孤儿对象，由生命周期规则与 GC 脚本兜底。反向做法会让用户看到不可自愈的播放 403。

### 4.3 各业务链路

**ffmpeg 类**（抽帧 / 裁剪 / VC 提音轨 / CC / 导出合并 / 连贯性预览）统一套 `workspace()`：fetch 输入 → 本地计算 → publish 输出 → 更新 DB。

导出合并需将整个项目全部分镜视频拉入 tmpdir 再 concat，20 个分镜可能达数 GB。需确认 tmpdir 所在卷空间，并使用流式下载而非全量读入内存。

**视频生成**（`agents/video_generator.py`）：Vertex 的 `types.Image.from_file()`（video_generator.py:161/176/194）需真实本地文件，首尾帧须先 `ws.fetch()`。

kie provider 的 `_upload_image()`（video_generator.py:289）现将本地帧 base64 上传至 kie CDN 换取临时 URL；素材上 OSS 后可直接传签名 URL 给 kie，省去一次上传。**列为可选优化，不与主改造绑定**。

**前端上传**（reference images / custom frames / 候选图临时参考）：`uploads.py` 收到 multipart 后写临时文件 → publish → DB 存 key。

**删除项目**：`delete_project_storage()`（storage.py:184）的 `shutil.rmtree` 换成 `delete_prefix("projects/<pid>/")`。OSS 批量删除单次上限 1000 个 key，需分页循环。

### 4.4 签名 URL 与前端时效性

`to_media_url(key)` 改为返回预签名 URL。

**关键事实**：签名是纯本地 HMAC 计算，不发网络请求。因此每次 API 响应现签一次成本约等于零——无需缓存或复用 URL。用户拿到响应即开始观看，几乎不可能过期；真正会过期的只有「页面长时间开着」这一种情况。

- TTL 取 **2 小时**
- SSE 推送的 URL 同样现签，每条推送都是新签，不存在推送陈旧 URL 的问题
- OSS 原生支持 Range 请求，签名 URL 亦然，进度条拖动无需额外处理
- **过期兜底**：前端 `<video>` 的 `onError` 触发时重新拉取项目接口换取新 URL。这是本次唯一需要改动前端的地方

### 4.5 现有 serve 路径的去向

| 现状 | 处理 |
|---|---|
| `main.py:135` `/api/media` StaticFiles 挂载 | 删除 |
| `main.py:144` `/api/media/*` 强制 no-cache 中间件 | 删除（key 唯一性已天然防缓存） |
| `assets.py:17` `serve_asset` | 保留路由，改为 302 重定向到签名 URL |
| `assets.py:48` `download_final` | 302 重定向，签名中带 `response-content-disposition`，由 OSS 直接返回附件下载头 |
| `storage.py:212` `validate_safe_path` | 换成 key 校验：禁 `..`、必须以 `projects/` 开头 |

附带效果：`validate_safe_path` 现用 `str.startswith` 判断路径包含关系（storage.py:223），`/storage-evil` 会被误判为位于 `/storage` 内。换成 key 校验后该问题自然消失。

---

## 5. 存量迁移

### 5.1 脚本

`backend/app/scripts/migrate_to_oss.py`，通过 `uv run --project backend python -m app.scripts.migrate_to_oss` 执行。四阶段可分别运行：

```
--scan      扫描 storage_root，输出待迁移清单（文件数、总大小、未被 DB 引用的量）
--upload    上传对象（可在线运行，不动 DB）
--backfill  回填 DB 路径字段 → key（需停写窗口）
--verify    校验 DB 中每个 key 在 OSS 真实存在
```

**幂等性为硬要求。** `--upload` 对每个文件先 `head_object` 比对大小 / CRC64，一致则跳过，中断后重跑只补差量。`--backfill` 依据值的形态判断是否已转换——以 `/` 开头为待转换的绝对路径，否则视为已是 key——因此同样可安全重跑。失败条目写入报告文件，支持单独重试。

### 5.2 需回填的字段

- `Shot`：`video_path`、`last_frame_path`、`custom_first_frame_path`、`target_last_frame_path`、`vc_audio_path`、**`custom_reference_paths`（JSON 数组）**
- `Project`：`storyboard_path`、`final_video_path`、`reference_voice_path`
- `ReferenceImage`：`storage_path`
- `ImageCandidate`：`file_path`、**`ref_paths`（JSON 数组）**

两个 JSON 数组字段容易遗漏，需单独处理。

**只此一次的时机**：4.1 新增的三列 `pre_vc_video_key` / `pre_cc_last_frame_key` / `pristine_last_frame_key`，初值只能通过扫描本地目录推导——前两者看 `output_pre_vc.mp4`、`last_frame_pre_cc.png` 是否存在，后者按 `pristine_last_frame_path()` 的原逻辑（`last_frame_*.png` 中排除固定名备份后取 mtime 最新）计算。本地文件一旦清理即无法恢复该信息，因此**这三列的填充必须在迁移脚本内完成**，不能留到后续补做。

### 5.3 上线切换顺序

字段语义切换是原子的，代码与数据必须同时切换：

```
1. 备份 DB（sqlite 直接 cp）+ 开启 bucket 版本控制
2. 在线运行 --upload（不停服，最耗时的一步在此消化）
3. 停止服务
4. 再次运行 --upload 补增量（停服前最后写入的文件）
5. 运行 --backfill
6. 部署新代码
7. 启动 + 运行 --verify
```

真正的停机窗口仅第 4~7 步，与数据量基本无关。

**回滚**：恢复 DB 备份 + 回滚代码，OSS 对象不删除。**迁移后本地 storage 目录先保留一到两周**作为回滚保险，确认稳定后再清理。

---

## 6. 错误处理

| 场景 | 处理 |
|---|---|
| **上传失败** | SDK 自带 `StandardRetryer`（3 次 + FullJitter）。调整 `retry_max_attempts=5`。**>100MB 使用 `upload_file` 分片上传管理器**而非 `put_object`，支持断点续传 |
| **下载失败** | `ws.fetch()` 失败即让任务失败并标记 shot 状态，**绝不静默降级**。明确失败可重试，优于悄悄使用错误文件 |
| **凭证失效** | ECS RAM Role 自动刷新；static AK 失效表现为全量 403。日志须区分「403 凭证问题」与「404 对象不存在」，并记录 OSS 返回的 **EC 码**（阿里云提供 EC 码自助诊断平台） |
| **tmpdir 空间不足** | 导出合并前检查可用空间是否足以容纳全部分镜，不足则明确报错。否则表现为 ffmpeg 神秘失败 |
| **签名 URL 403** | 前端 `onError` 重新拉取接口换取新 URL（见 4.4） |

**孤儿对象 GC**：4.2 的一致性规则会持续产生孤儿。清理脚本比对 OSS `list_prefix` 与 DB 引用，识别超过 N 天未被引用的对象。**本次仅实现 dry-run 报告，不执行删除**——它是整套设计中唯一具备不可逆破坏风险的组件，先观察实际孤儿量再决定是否开启自动清理。

**运维侧配置**（非代码，但属设计的一部分）：

- bucket 开启版本控制（GC 的误删保险）
- 配置生命周期规则（历史版本 30 天后转低频或删除）
- bucket 保持私有读
- RAM 策略仅授予目标 bucket 的读写权限，**不使用 `AliyunOSSFullAccess`**

**日志**沿用项目现有 `python-json-logger`，每次对象操作记录 key、操作类型、耗时、字节数、EC 码。Langfuse 用于 LLM 追踪，不纳入此处。

---

## 7. 测试策略

项目规则明确：除会计费的模型调用外，一律不 mock。OSS 是真实基础设施而非计费模型边界，**因此测试必须打真实 OSS**，不实现任何 fake object store。

**隔离方式**：dev bucket + 每次测试运行使用唯一前缀 `test/<run_id>/`，teardown 删除整个前缀。测试文件为 KB~MB 级，费用可忽略。

| 层次 | 做法 |
|---|---|
| **key 函数单元测试** | 改造后为纯函数，直接断言 key 拼接，无需网络 |
| **object_store / workspace** | 打真实 dev bucket：put/get/copy/delete/list/签名 |
| **签名 URL** | **断言真能 `GET` 到内容**，而非断言 URL 字符串形态——签名算错时字符串形态依然正确 |
| **ffmpeg 链路集成测试** | 复用已有真实 `output_<ts>_<uuid>.mp4` 素材（项目 e2e 规则推荐的做法），走完整 fetch → ffmpeg → publish → DB，**不调用任何模型** |
| **迁移脚本** | 临时 storage 目录 + 临时 DB 跑全流程，重点验证幂等：**连跑两次结果完全一致** |
| **e2e (Playwright)** | 真实后端 + 真实 DB + 真实 OSS，仅短路项目规则中列明的 AI 触发端点 |

后端测试用 `uv run pytest` 直接运行，不套 podman。需要 OSS 凭证的测试打 marker，便于无凭证环境跳过。

---

## 8. 分阶段落地

DB 字段语义切换是原子的，生产环境只有一次切换窗口（见 5.3）。以下为**代码落地顺序**，每阶段可独立验证，在 dev bucket + 全新 dev DB 上跑通，最后一次性切换生产。

| 阶段 | 内容 | 验证方式 |
|---|---|---|
| **0** | 依赖、config 字段、secrets 接线、`oss_client`、`object_store` | 冒烟脚本：连上 dev bucket 完成传/取/签/删。**不碰业务代码** |
| **1** | `workspace()` 原语 + `storage.py` key 函数（新增，旧函数暂留） | 单元 + 集成测试 |
| **2** | 新增 `pre_vc_video_key` / `pre_cc_last_frame_key` / `pristine_last_frame_key` 三列 + alembic 迁移 | 迁移可正反向执行 |
| **3** | **写路径**改造：生成、上传、各 ffmpeg 链路，产出并存 key | 集成测试走真实素材 |
| **4** | **读路径**改造：`to_media_url` 签名、`assets.py` 改 302、删 `/api/media` 挂载 | e2e 真实播放 |
| **5** | 前端 `onError` 重拉换新 URL | e2e 模拟过期 |
| **6** | 迁移脚本四阶段 + `--verify` | 临时 storage/DB 幂等测试 |
| **7** | **生产切换**（5.3 的七步） | `--verify` 全绿 + 人工抽查播放 |
| **8** | 孤儿 GC 脚本（仅 dry-run） | 输出报告，人工核对 |

阶段 3 必须先于阶段 4：读路径能工作的前提是写路径已在产出 key。

---

## 9. 开发期协作风险

按项目部署约定，**所有 worktree 共用同一套 `deploy_app-data`（DB）与 `deploy_app-storage`（媒体）卷**，同一时刻仅一个栈运行。

风险：**一旦在某 worktree 运行 `--backfill`，共享 dev DB 中的字段即变为 key。此时将栈切回任何旧 worktree，旧代码会把 key 当绝对路径使用，媒体全部加载失败。**

**已采纳的处理方式**：开发期间为本 worktree 使用**独立的 DB 卷与独立 bucket**，完全不触碰共享卷。代价仅是 compose 中多一组卷名，换来整个开发过程零风险。

---

## 10. 实施阶段需要逐行核对的代码

设计层面无未定项。以下几处依赖具体代码细节，在写实施计划时需逐行核对，但均不影响架构选择：

1. **全部素材读写点的清单** — 需完整枚举 `worker/tasks.py`、`app/api/pipeline.py`、`app/api/image_candidates.py`、`app/api/voice.py`、`app/api/uploads.py`、`app/agents/` 中所有写入或读取素材文件的位置，确保阶段 3/4 无遗漏。遗漏一处即为一条运行时故障。
2. **CLAUDE.md「shot 素材文件变更审计」清单的逐条复核** — 裁剪 / 还原 / VC / CC 各自需要清理的关联文件与需重置的 status 字段，在改为 key + DB 列后要重新走一遍该检查清单。
3. **`_reset_tail_frame()`（pipeline.py:44）等「路径即真相」的辅助函数** — 该函数注释明确写着 "Path-as-truth: a tail frame is used iff target_last_frame_path is set"。此处语义在 key 化后依然成立（字段有值 = 有尾帧），但需确认同类模式没有别处依赖文件真实存在性。
