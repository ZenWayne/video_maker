# PRD：多租户与用户计费

| 项目 | 内容 |
|------|------|
| 文档状态 | Draft — 待评审 |
| 创建日期 | 2026-08-07 |
| 作者 | Wayne |
| 相关代码 | `backend/app/`、`frontend-vite/src/` |
| 代码基线 | `997ab77`（PR #38 合并后） |
| 关联文档 | `CLAUDE.md`（Secrets 管理、素材文件审计规则） |

---

## 1. 背景

video_maker 目前是一个**单租户、无认证**的内部工具。所有人共享同一个 SQLite 库和同一个存储根目录，唯一的"归属"信号是 `Project.creator_name` —— 一段由前端自由填写、后端不做任何校验的文本。

同时，产品的每一次操作背后都是**真金白银的上游调用**：Veo 视频生成（按秒计费，成本占绝对大头）、Gemini 文本与图像、DeepSeek，以及自建 GPU 上的 CosyVoice 音色转换。目前这些消耗**完全没有计量、没有归属、没有上限**——单个用户可以无成本地把账单打到任意高度。

要把它变成一个可以对外开放的产品，必须同时解决两件事：**谁的数据只有谁能看**，以及**谁花的钱谁来付**。

## 2. 目标与非目标

### 2.1 目标

1. **把数据库从 SQLite 迁移到 PostgreSQL**，为计费账本提供真正的事务与行级锁能力。
2. 引入账号体系，**一个用户即一个租户**，项目及其全部素材严格归属到用户。
3. 对**视频生成与图像生成**两类模型调用做计量，按**固定定价表**折算为点数。
4. 实现**预付点数**账本：余额不足则拒绝执行，从机制上杜绝欠费。
5. 通过 **Creem** 完成点数充值，覆盖下单、回调入账、退款/拒付冲正。
6. 用户可自助查看余额、消费明细与用量看板。

### 2.2 非目标（本期不做）

- **组织/团队租户**。用户选择了「个人用户即租户」粒度。数据模型会为未来的组织层预留（见 §7.1），但本期不实现成员、角色、共享。
- **后付费、订阅套餐、超量计费**。本期只做预付点数。
- **对文本类 LLM 调用计费**。编剧、导演、分镜编辑的 token 成本相对视频生成可忽略不计（量级差约 1000 倍），v1 定价表一律记 0 点。**这是刻意的简化，不是遗漏**：它让计费只需要接入 2 个调用点而不是 8 个。
- **对自建算力计费**。音色转换（CosyVoice）与内容分析转写（faster-whisper）都跑在自有 GPU 上，没有按次上游账单，v1 不计费。
- **存储计费**。v1 只做配额限制，不按 GB·月 收费。
- **发票与税务**。Creem 是 Merchant of Record（记录商户），代收代缴与开票由 Creem 承担，我们不自建发票系统。
- **接入 Creem 以外的支付渠道**（微信/支付宝/Stripe 直连）。
- **多区域部署、租户级数据库拆分**。本期为共享库 + 行级租户隔离。

## 3. 术语

| 术语 | 定义 |
|------|------|
| 租户 (Tenant) | 本期等同于一个用户账号。所有业务数据的隔离边界。 |
| 点数 (Credit) | 平台内的预付计价单位，**整数**，不允许小数。定价锚见 §10.1。 |
| 冻结 (Hold) | 提交异步任务时预扣的点数，尚未真正消费，可释放。 |
| 扣减 (Capture) | 任务成功后把冻结转为实际消费。 |
| 释放 (Release) | 任务失败/取消时把冻结退回可用余额。 |
| 计费操作 (Billable Op) | 一次会产生上游成本的动作，如"生成一个 8 秒分镜"。 |
| 定价版本 (Pricing Version) | 一份定价表的不可变快照，盖在每条用量记录上，保证历史账目可复算。 |

---

## 4. 现状差距（代码级证据）

本节是需求的依据，每一条都对应真实代码位置。

| # | 现状 | 位置 | 风险 |
|---|------|------|------|
| G1 | **完全没有认证/授权**。全部 API 匿名可访问。 | `backend/app/main.py:136-144` 挂载的所有 router | 阻断级 |
| G2 | 资产接口只做了文件名净化与 key 合法性校验，**没有任何归属校验**。任何人拿到 `project_id` 即可换取他人素材的签名 URL。 | `backend/app/api/assets.py:20-45` `serve_asset()` | **阻断级，最高优先** |
| G3 | 签名 URL 默认 **7200 秒（2 小时）**有效期，且签发后无法吊销。一旦泄露，2 小时内可被任意转发。 | `backend/app/config.py:88` `cos_signed_url_ttl_sec: int = 7200` | 高 |
| G4 | 项目归属是自由文本，客户端可任意伪造，列表过滤形同虚设。 | `backend/app/models/project.py:55`；`backend/app/api/projects.py:114,178` | 阻断级 |
| G5 | SSE 推送按 `project_id` / `analysis_id` 订阅，无归属校验，可实时窃听他人任务状态。 | `backend/app/api/stream.py`、`content_analysis.py:134` | 阻断级 |
| G6 | MCP server 以 HTTP 客户端身份直连后端，**不携带任何身份**。 | `backend/mcp_server/client.py:32-106` | 阻断级 |
| G7 | 所有上游调用**无任何计量埋点**。Langfuse 只覆盖 LLM 调用，Veo、VC、内容分析均未纳入。 | `backend/app/agents/llm.py`、`video_generator.py`、`worker/tasks.py` | 阻断级 |
| G8 | **`ContentAnalysis` 连 `creator_name` 都没有**——是一个完全无归属字段的实体，且自带转写 + LLM 分析的上游成本。 | `backend/app/models/project.py:236-260`；`backend/app/api/content_analysis.py` | 阻断级 |
| G9 | ARQ worker 池全局共享（`WORKER_POOL_SIZE: "4"`），单租户可长期占满队列饿死其他人。 | `deploy/config.yml`、`backend/worker/arq_worker.py` | 高 |
| G10 | COS 存储无配额、无生命周期，对象存储账单会被无限增长的 mp4 推高。 | `backend/app/services/object_store.py` | 中 |

> **注 1**：G2 是最需要优先修的一项。它不依赖计费，且当前就是一个可被利用的数据泄露面。
>
> **注 2 — 已有的良好基础**：媒体本体已迁移到腾讯云 COS（`object_store.py` / `cos_client.py`），`serve_asset()` 已改为 302 重定向到预签名 URL，后端不再中转流量。**签名 URL 的机制已经就位**，本 PRD 不需要新建，只需在签发前补上归属校验并收紧 TTL。同理，`delete_project` 已经调用 `object_store.delete_prefix()` 清理对象存储（`backend/app/api/projects.py:375`），级联删除是完整的。

---

## 5. 用户故事

**注册用户**
- 作为新用户，我可以用邮箱+密码注册并登录，注册后获得一笔新手赠送点数，让我能完整跑通一个短项目再决定是否付费。
- 作为用户，我在项目列表里**只能**看到自己的项目，也只能播放/下载自己的素材。

**计费透明**
- 作为用户，我在点「开始生成」之前就能看到**这次要花多少点数**，以及执行后余额会剩多少。
- 作为用户，当余额不足时，系统在**扣费前**就拦下来并提示我充值，而不是跑到一半失败。
- 作为用户，如果生成因为系统/上游故障失败了，**不应该扣我的点数**。
- 作为用户，我能查到每一笔消费花在了哪个项目、哪个分镜、哪种操作上。

**充值**
- 作为用户，我可以选一个点数包，跳转到 Creem 完成支付，回来后余额已经到账。
- 作为用户，如果支付成功但页面卡住了，我刷新后余额依然正确（不重复入账、不丢账）。

**运营**
- 作为运营，我能看到每个用户的余额、消费、上游真实成本与毛利。
- 作为运营，我能手工给用户调整点数（补偿、赠送），且每笔调整都有操作人和原因留痕。

---

## 6. 功能需求

### F1 账号与认证

- **F1.1** 邮箱 + 密码注册。密码用 Argon2id 哈希（不落明文、不可逆）。
- **F1.2** 邮箱验证：注册后发送验证链接，未验证账号可登录但**不能执行任何计费操作**（防止用一次性邮箱刷新手赠送点数）。
- **F1.3** 登录签发短时效 access token（15 分钟）+ 长时效 refresh token（30 天），均以 `HttpOnly; Secure; SameSite=Lax` Cookie 下发。
- **F1.4** 登出使 refresh token 失效（服务端维护撤销列表）。
- **F1.5** 忘记密码：邮件重置链接，令牌单次有效、30 分钟过期。
- **F1.6** MCP / 自动化场景使用**长期 API Key**（`vmk_` 前缀，仅创建时明文展示一次，库里存哈希），绑定到用户，可单独吊销。解决 G6。

### F2 租户数据隔离

- **F2.1** `Project` 增加 `owner_id`（FK → `users.id`，`NOT NULL`）。`creator_name` **降级为展示名快照**，不再承担任何权限语义。
- **F2.2** `ContentAnalysis` 同样增加 `owner_id`（FK → `users.id`，`NOT NULL`）。它当前没有任何归属字段（G8），是隔离工作中最容易被遗漏的一块。
- **F2.3** 所有业务读写接口，统一经由一个依赖注入的 `get_current_user()`，并在查询层强制拼接 `WHERE owner_id = :current_user`。
  - 非本人资源一律返回 **404**（不是 403），避免通过状态码探测资源是否存在。
  - 覆盖面必须包含：`projects`、`pipeline`、`voice`、`uploads`、`assets`、`stream`、`image_candidates`、`content_analysis` 全部 8 个 router。
- **F2.4** **复用已有的 COS 签名 URL 机制**（`object_store.signed_url()`），不新建。改动只有两点：
  1. `serve_asset()` / `download_final()` 在**签发之前**校验登录态与资源归属（G2）；
  2. 把 `cos_signed_url_ttl_sec` 从 7200 秒收紧到 **600 秒**（G3），并允许下载类请求单独传更长 TTL。
  > 签名 URL 本身无法吊销，因此 TTL 是唯一的止损手段。10 分钟足够覆盖播放器起播与断点续传，又把泄露窗口压到可接受范围。
- **F2.5** SSE 订阅前校验资源归属（G5），非本人直接 403 断流。项目流与内容分析流都要覆盖。
- **F2.6** **COS key 前缀不变**（仍为 `project_prefix(project_id)` 派生）。
  > **决策理由**：改前缀会触碰 `shot_prefix()` / `final_video_key()` 等一整条素材读写链，按 `CLAUDE.md` 的《Shot 素材文件变更审计》规则需要全量审计所有下游读取方，风险与收益不成正比。`project_id` 是 UUID，本身不可枚举；隔离由 F2.3/F2.4 在访问层保证。租户存储用量改为在 DB 计数器中维护（见 F10），不依赖 key 前缀。

### F3 用量计量

**v1 只对两类模型调用计量**。这把计费的接入面从散落在 8 处的调用点压缩到 2 处，是本期能快速落地的关键。

- **F3.1** 每一次计费操作在**完成时**写一条 `usage_records`，字段见 §7.5。
- **F3.2** 计量口径：

  | 操作 | 计量单位 | 数量来源 | 代码位置 |
  |------|----------|----------|----------|
  | **视频生成** | 秒 | `Shot.shot_duration`（4/6/8） | `app/agents/video_generator.py` |
  | **图像生成** | 张 | 生成的候选图数量 | `app/services/image_generation.py` |

  其余全部操作（剧本、导演、分镜编辑、音色转换、内容分析、导出）**不计量、不计费**，理由见 §2.2。

- **F3.3** 同时记录**上游真实成本**（`upstream_cost_usd`），仅用于内部毛利报表，不对用户展示。按上游单价 × 数量估算。
- **F3.4** 每条用量记录关联 Langfuse `trace_id`，便于从账单反查到具体一次调用。
- **F3.5** Langfuse 的 `session_id` 必须带租户前缀（`u{user_id}:{project_id}`），避免跨租户内容在可观测面板中混淆。
- **F3.6** 未计费的 LLM 成本仍可在 **Langfuse 里直接观测**（已接入，见 `app/observability.py`），不需要为它们额外建埋点。若日后发现文本成本占比上升，再以新定价版本纳入即可，数据模型无需改动。

### F4 点数账本（核心）

账本是**只追加**的。余额是账本的物化结果，不是唯一真相。

- **F4.1** 每个用户有且仅有一个 `credit_accounts` 行，含 `balance`（可用）与 `held`（冻结）两个整数字段。
- **F4.2** 所有余额变更都写一条 `credit_transactions`，并在同一个数据库事务内更新 `credit_accounts`。
- **F4.3** **两阶段扣费（Hold → Capture / Release）**，这是设计的关键：

  ```
  提交任务  →  HOLD    balance -= N,  held += N      （余额不足则整体拒绝）
  任务成功  →  CAPTURE held -= N,  实际消费 = N'     （N' 可 ≤ N，差额退回 balance）
  任务失败  →  RELEASE held -= N,  balance += N      （用户不为失败买单）
  ```

  理由：视频生成是异步的、耗时数分钟、且会失败。单阶段"先扣后退"在并发下会让用户看到余额跳变、且失败退款容易漏；单阶段"完成后扣"则允许用户在余额不足时把任务全部提交出去，形成透支。两阶段同时解决这两个问题。

- **F4.4** 每条交易带 `idempotency_key`（唯一索引）。重复提交（用户双击、webhook 重投、worker 重试）只生效一次。
- **F4.5** 冻结有**超时兜底**：worker 崩溃导致既没 capture 也没 release 的冻结，由一个定时任务在 24 小时后自动 release，并记 `RELEASE_TIMEOUT` 类型便于排查。
- **F4.6** 余额允许因**退款/拒付冲正**而变为负数。负余额下禁止一切计费操作，但不影响读取已有数据。
- **F4.7** 并发安全：`credit_accounts` 更新使用 PostgreSQL 行级锁（`SELECT ... FOR UPDATE`）。
  > 这是 §12.1 的 PostgreSQL 迁移必须先行的直接原因：SQLite 没有真正的行级锁，`SELECT ... FOR UPDATE` 在其上是空操作，两个并发扣费请求会双花。

### F5 定价表

- **F5.1** 定价表以**配置**形式存在（`deploy/config.yml` 之外的独立 `pricing.yml`，随代码版本化），不硬编码在业务代码里。
- **F5.2** 每份定价表有一个单调递增的 `pricing_version`。
- **F5.3** 每条 `usage_records` 与 `credit_transactions` 都盖上生成时的 `pricing_version`，历史账目可原样复算。
- **F5.4** 调价只能通过新增版本生效，**永不修改已发布的版本**。
- **F5.5** 定价表草案见 §10。

### F6 额度预检与拦截

- **F6.1** 触发**视频生成或图像生成**的接口，在入队**之前**完成：估价 → 校验余额 → 冻结。任一步失败则不入队。其余接口（剧本、导演、编辑、VC、内容分析）不接计费，保持现状。
- **F6.2** 余额不足返回 `402 Payment Required`，响应体含 `required`、`available`、`shortfall` 三个数值，前端据此弹出充值引导。
- **F6.3** 新增只读估价接口 `POST /api/billing/estimate`，前端在按钮旁实时展示"本次将消耗 N 点，余额剩余 M 点"。
- **F6.4** 批量操作（如"重新生成全部分镜"）按**整批**估价与冻结，避免跑到一半没钱造成半成品。
- **F6.5** **失败归因决定是否收费**：
  - 上游 5xx、超时、内容安全拦截、我方 bug → **不收费**（RELEASE）；
  - 用户主动取消且任务尚未真正提交给上游 → **不收费**；
  - 任务已提交上游且上游已产出（哪怕用户不满意）→ **收费**（CAPTURE）。
  - 该归因由 worker 在标记失败时显式给出，不允许默认值。

### F7 Creem 充值

**已核实的 Creem 契约**（2026-08-07）：

| 项 | 值 |
|----|-----|
| 生产 base URL | `https://api.creem.io` |
| 测试 base URL | `https://test-api.creem.io` |
| 鉴权头 | `x-api-key` |
| 创建结账 | `POST /v1/checkouts` |
| 请求字段 | `product_id`(必填)、`request_id`、`customer{id\|email}`、`metadata`、`success_url`、`units` |
| 响应 | `checkout_url`（跳转地址）、`id`、`status`、`order`、`customer`、`metadata` 等 |
| Webhook 签名头 | `creem-signature`（HMAC-SHA256，对**原始请求体**计算） |
| Webhook 重试 | 渐进退避 30 秒 / 1 分钟 / 5 分钟 / 1 小时；需回 `200` |

- **F7.1** 在 Creem 后台把每个点数包配成一个 product；我方 `credit_packs` 表存 `creem_product_id` 与对应点数。
- **F7.2** 用户点充值 → 后端先落一条 `payment_orders`（状态 `pending`）→ 以该订单 ID 作为 `request_id` 调用 `POST /v1/checkouts` → 返回 `checkout_url` 供前端跳转。
  - `metadata` 带上 `{user_id, pack_id, order_id}`，回调时用于对账。
- **F7.3** Webhook 端点 `POST /api/billing/webhooks/creem`：
  - **必须**读取**原始字节**做 HMAC-SHA256 校验（不能先经过 JSON 反序列化再重新序列化），用常量时间比较；
  - 校验失败返回 401 且不做任何处理；
  - 校验通过后**先持久化原始事件**（`payment_events` 表，以 Creem 事件 `id` 建唯一索引）再处理业务，保证可重放、可审计；
  - 处理完立即返回 200；重逻辑异步化，避免触发 Creem 重试。
- **F7.4** 事件处理：
  | 事件 | 处理 |
  |------|------|
  | `checkout.completed` | 订单置 `paid`，按包面额 `GRANT` 点数入账 |
  | `refund.created` | `REFUND_CLAWBACK` 冲正对应点数，允许余额为负 |
  | `dispute.created` | 同上冲正，并将账号置为 `restricted`，禁止继续消费，转人工 |
- **F7.5** **幂等性**：入账以 Creem 事件 `id` 作为 `idempotency_key`。同一事件重投多次只入账一次。
- **F7.6** **不信任前端回跳**。`success_url` 只负责展示"支付处理中"并轮询余额；点数入账**唯一**依据是 webhook。
- **F7.7** 兜底对账：定时任务扫描超过 30 分钟仍为 `pending` 的订单，主动查询 Creem 订单状态补齐（防 webhook 丢失）。
- **F7.8** `creem_api_key` 与 `creem_webhook_secret` 按 `CLAUDE.md` 的 K8s 风格密钥规范管理：写入 `secrets.yml`、在 `deploy/secrets.yml.example` 加占位、在 compose 的 `secrets:` 段声明。**不得**出现在代码或 `config.yml` 中。

### F8 用量与账单可视化

- **F8.1** 「我的账户」页：当前余额、冻结中点数、近 30 天消费趋势。
- **F8.2** 消费明细列表：时间、操作类型、项目/分镜、消耗点数，支持按项目筛选与分页。
- **F8.3** 充值记录列表：时间、点数包、金额、状态、Creem 订单号。
- **F8.4** 项目详情页展示**该项目累计消耗点数**。

### F9 公平调度与并发配额（G9）

- **F9.1** 每个用户设 `max_concurrent_jobs`（默认 2）。超出则任务排入该用户自己的等待队列，不占用全局 worker 槽位。
- **F9.2** ARQ 入队时按用户轮转（round-robin），避免单租户批量提交把 4 个 worker 槽位全部占满。
- **F9.3** 触达并发上限时前端明确提示"你有 N 个任务排队中"，而非静默等待。

### F10 存储配额（G10）

- **F10.1** `users.storage_bytes_used` 计数器，在 `object_store.put()` / `delete()` / `delete_prefix()` 前后增减。
- **F10.2** 每个用户有存储配额（默认 20 GB）。超限时禁止新建项目与新生成，但允许删除与导出。
- **F10.3** v1 **不对存储收费**，只做硬性配额限制。超限即拦截，不产生点数消耗。
- **F10.4** 项目/分析删除时回退计数器。
  > **对象清理已经是完整的**：`delete_project` 已调用 `object_store.delete_prefix(project_prefix(...))`（`backend/app/api/projects.py:375`），本条只需补计数器回退，不需要新写清理逻辑。
- **F10.5** 每日一次用 `object_store.list_prefix()` 重算真实占用并校正计数器漂移。

### F11 管理后台（最小实现）

- **F11.1** 用户列表：余额、累计消费、累计充值、存储占用、状态。
- **F11.2** 手工调整点数（`ADJUST` 类型），**必须**填写操作人与原因，全部留痕。
- **F11.3** 毛利报表：按操作类型汇总「收取点数折算收入」vs「上游真实成本」。
- **F11.4** 封禁/解封账号。

---

## 7. 数据模型

### 7.1 `users`

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | String(36) PK | UUID |
| `email` | Text UNIQUE NOT NULL | |
| `password_hash` | Text NOT NULL | Argon2id |
| `display_name` | Text NOT NULL | 取代 `creator_name` 的展示来源 |
| `email_verified_at` | DateTime NULL | 未验证不可计费消费 |
| `status` | String(20) | `active` / `restricted` / `banned` |
| `org_id` | String(36) NULL | **本期恒为 NULL**，为未来组织租户预留 |
| `max_concurrent_jobs` | Integer | 默认 2 |
| `storage_quota_bytes` | BigInteger | 默认 20 GB |
| `storage_bytes_used` | BigInteger | 计数器 |
| `created_at` / `updated_at` | DateTime | |

### 7.2 `api_keys`

`id`、`user_id` FK、`name`、`key_hash`、`prefix`、`last_used_at`、`revoked_at`、`created_at`。

### 7.3 `credit_accounts`

| 字段 | 类型 | 说明 |
|------|------|------|
| `user_id` | String(36) PK FK | 一对一 |
| `balance` | BigInteger NOT NULL | 可用点数，可为负 |
| `held` | BigInteger NOT NULL | 冻结中，恒 ≥ 0 |
| `lifetime_granted` / `lifetime_spent` | BigInteger | 统计用 |
| `version` | Integer | 乐观锁 |
| `updated_at` | DateTime | |

**不变式**：`held == SUM(未结冻结)`，且 `balance + held == SUM(所有交易 amount)`。由日终对账任务校验，不一致则告警。

### 7.4 `credit_transactions`（只追加）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | String(36) PK | |
| `user_id` | String(36) FK | |
| `type` | String(24) | `GRANT_SIGNUP` / `GRANT_PURCHASE` / `HOLD` / `CAPTURE` / `RELEASE` / `RELEASE_TIMEOUT` / `REFUND_CLAWBACK` / `ADJUST` |
| `amount` | BigInteger | 有符号；对余额的净影响 |
| `balance_after` | BigInteger | 快照，便于审计 |
| `hold_id` | String(36) NULL | CAPTURE/RELEASE 指回原 HOLD |
| `ref_type` / `ref_id` | String | `usage` / `payment_order` / `admin` |
| `idempotency_key` | Text UNIQUE | 幂等锚 |
| `pricing_version` | Integer NULL | |
| `operator` / `reason` | Text NULL | 仅 `ADJUST` 必填 |
| `created_at` | DateTime | |

索引：`(user_id, created_at DESC)`、`UNIQUE(idempotency_key)`。

### 7.5 `usage_records`

`id`、`user_id`、`project_id`、`shot_id` NULL、`op_type`、`provider`（vertex/gemini/deepseek/cosyvoice）、`model`、`quantity`、`unit`（second/image/call/gb_month）、`credits_charged`、`upstream_cost_usd`、`pricing_version`、`langfuse_trace_id`、`created_at`。

索引：`(user_id, created_at DESC)`、`(project_id)`。

### 7.6 `credit_packs`

`id`、`name`、`credits`、`price_amount`、`price_currency`、`creem_product_id`、`is_active`、`sort_order`。

### 7.7 `payment_orders`

`id`（即 Creem `request_id`）、`user_id`、`pack_id`、`credits`、`amount`、`currency`、`status`（`pending`/`paid`/`failed`/`refunded`/`disputed`）、`creem_checkout_id`、`creem_order_id`、`checkout_url`、`paid_at`、`created_at`。

### 7.8 `payment_events`

`id`（Creem 事件 ID，PK）、`event_type`、`raw_payload`（原文）、`signature_valid`、`processed_at`、`process_error`、`received_at`。原始事件永久保留，供对账与纠纷举证。

### 7.9 现有表变更

**`projects`**：新增 `owner_id String(36) NOT NULL FK → users.id`，新增索引 `ix_projects_owner_created (owner_id, created_at DESC)`。`creator_name` 保留但语义变更为展示快照；`ix_projects_creator_name` 索引废弃。

**`content_analyses`**：新增 `owner_id String(36) NOT NULL FK → users.id`，新增索引 `ix_content_analyses_owner_created (owner_id, created_at DESC)`。该表原本**没有任何归属字段**，是本期隔离工作中最容易漏掉的一处。

`reference_samples`、`shots`、`reference_images`、`image_candidates`、`events` 均通过父表间接归属，**不**冗余 `owner_id`，避免双写不一致。

---

## 8. API 设计

### 认证
```
POST   /api/auth/register          {email, password, display_name}
POST   /api/auth/login             {email, password}          → 下发 Cookie
POST   /api/auth/logout
POST   /api/auth/refresh
POST   /api/auth/verify-email      {token}
POST   /api/auth/forgot-password   {email}
POST   /api/auth/reset-password    {token, new_password}
GET    /api/auth/me                → 当前用户 + 余额摘要
```

### API Key
```
GET    /api/auth/api-keys
POST   /api/auth/api-keys          {name}   → 明文 key 仅此一次返回
DELETE /api/auth/api-keys/{id}
```

### 计费
```
POST   /api/billing/estimate       {op_type, params}  → {credits, balance_after}
GET    /api/billing/account        → {balance, held, quota...}
GET    /api/billing/transactions   ?page&type
GET    /api/billing/usage          ?project_id&from&to
GET    /api/billing/packs          → 可售点数包
POST   /api/billing/checkout       {pack_id}          → {checkout_url, order_id}
GET    /api/billing/orders         ?page
POST   /api/billing/webhooks/creem → Creem 回调（免登录，靠签名鉴权）
```

### 资产（**无新增端点**）
```
GET    /api/projects/{pid}/assets/{kind}/{file}   → 已存在；补登录态 + 归属校验后再 302 至 COS 签名 URL
GET    /api/projects/{pid}/final.mp4              → 同上
```
> 签名与下发由 `object_store.signed_url()` 承担，本期不新建签名端点，只在现有端点前加校验。

### 管理
```
GET    /api/admin/users            ?q&page
POST   /api/admin/users/{id}/credits   {amount, reason}
POST   /api/admin/users/{id}/status    {status}
GET    /api/admin/margin-report        ?from&to
```

**全局变更**：现有全部 8 个 router — `projects`、`pipeline`、`voice`、`uploads`、`assets`、`stream`、`image_candidates`、`content_analysis` — 一律加上认证依赖与归属校验。`debug` router 应在生产环境直接禁用。

---

## 9. 关键流程

### 9.1 生成一个分镜（含冻结/扣减）

```
用户点「生成分镜 3」
  → POST /api/projects/{pid}/shots/3/generate
  → 校验登录 + 项目归属（否则 404）
  → 校验邮箱已验证、账号非 restricted/banned
  → 估价：shot_duration=8s → 查定价表 v3 → 120 点
  → 事务开始
      SELECT ... FOR UPDATE credit_accounts
      balance(500) >= 120 ?  否 → 402 {required:120, available:X, shortfall:Y}
      写 HOLD 交易 (idempotency_key = "gen:{pid}:{shot}:{attempt_uuid}")
      balance 500→380, held 0→120
    事务提交
  → ARQ 入队（payload 带 hold_id）
  → 202 {status: "queued", credits_held: 120}

worker 执行
  ├─ 成功 → 写 usage_record（quantity=8s, credits=120, upstream_cost_usd, trace_id）
  │        → CAPTURE：held 120→0，lifetime_spent += 120
  ├─ 上游失败/超时 → RELEASE：held 120→0, balance 380→500
  └─ 崩溃无响应 → 24h 后 RELEASE_TIMEOUT 兜底
```

### 9.2 充值

```
用户选「500 点包 / ¥50」
  → POST /api/billing/checkout {pack_id}
  → 落 payment_orders(status=pending, id=ORD-xxx)
  → POST https://api.creem.io/v1/checkouts
       headers: x-api-key: <secret>
       body: {product_id, request_id: "ORD-xxx",
              customer:{email}, metadata:{user_id, pack_id, order_id},
              success_url: "https://app/.../billing/return?order=ORD-xxx"}
  → 返回 checkout_url，前端跳转

用户在 Creem 完成支付
  → Creem POST /api/billing/webhooks/creem
  → 读原始 body，HMAC-SHA256 校验 creem-signature（常量时间比较）
  → 以事件 id 写入 payment_events（唯一索引；已存在则直接 200 返回）
  → 订单置 paid，写 GRANT_PURCHASE 交易（idempotency_key = 事件 id）
  → 返回 200

用户被重定向回 success_url
  → 页面显示「支付处理中」，轮询 GET /api/billing/account
  → 余额到账后刷新展示
```

### 9.3 退款 / 拒付

```
refund.created / dispute.created
  → 校验签名 → 落 payment_events
  → 写 REFUND_CLAWBACK（amount 为负），余额可变负
  → dispute 额外将 users.status 置 restricted
  → 通知运营人工跟进
```

---

## 10. 定价表草案（v1）

> ⚠️ **本节数值为待确认草案**。上游单价来自公开资料而非官方账单，**必须**在实施前用 Vertex AI / Gemini / DeepSeek 官方定价页与实际账单校准，并由财务确认目标毛利率。定价表一经发布即冻结，调价走新版本。

### 10.1 计价锚

**1 点 = ¥0.10**。选整数点数而非直接展示金额，是为了让调价与汇率波动不直接冲击用户心智。

**取整规则**：点数恒为整数。任何按连续量（秒、分钟、GB）计费的操作，先把数量**向上取整到计价单位**再乘单价，结果必然为整数。永不产生小数点数，也就不存在四舍五入误差累积。

成本推导示例（Veo 3.1 Fast，720p，`veo-3.1-fast-generate-001`）：

```
上游 ≈ $0.10/秒 → 8 秒 ≈ $0.80 ≈ ¥5.8（按 7.2 汇率）
目标毛利率 50% → 售价 ≈ ¥11.6 → 取整 120 点（¥12）
```

### 10.2 定价表

**收费项（全部为模型调用）**

| 操作 | 模型 | 单位 | 点数 | 折合 | 上游成本估算 |
|------|------|------|------|------|--------------|
| 视频生成 4 秒 | `veo-3.1-fast-generate-001` | 次 | 60 | ¥6.0 | ≈ ¥2.9 |
| 视频生成 6 秒 | 同上 | 次 | 90 | ¥9.0 | ≈ ¥4.3 |
| 视频生成 8 秒 | 同上 | 次 | 120 | ¥12.0 | ≈ ¥5.8 |
| 图像生成（首帧 / 尾帧 / 角色校准 / 候选图） | Gemini 图像 | 张 | 7 | ¥0.7 | ≈ ¥0.28 |

**免费项（v1 一律记 0 点）**

剧本生成、分镜生成/重生成、分镜文本编辑、音色转换、内容分析（转写 + brief）、导出合并、存储。理由见 §2.2。

**观察**：视频生成占单项目成本的 95%+，只对视频与图像收费在经济上已经覆盖绝大部分成本。一个 10 分镜、每镜 8 秒、配 20 张图的项目 = 10×120 + 20×7 = **1340 点 ≈ ¥134**。定价与新手赠送额度都应围绕这个数字设计。

### 10.3 点数包草案

| 包名 | 点数 | 价格 | 单价 |
|------|------|------|------|
| 体验包 | 300 | ¥30 | ¥0.100/点 |
| 标准包 | 1,000 | ¥95 | ¥0.095/点 |
| 专业包 | 3,000 | ¥270 | ¥0.090/点 |
| 工作室包 | 10,000 | ¥850 | ¥0.085/点 |

**新手赠送**：邮箱验证后赠 **150 点**（够生成 1 个 8 秒分镜 + 若干张图，足以跑通完整流程但不足以白嫖成片）。

---

## 11. 非功能需求

### 11.1 安全

- 所有密钥（`creem_api_key`、`creem_webhook_secret`、`asset_signing_key`、`jwt_secret`）走 `CLAUDE.md` 的 K8s 风格密钥流程：`secrets.yml` → `make secrets` → compose `secrets:` 挂载。**禁止**进入代码、`config.yml` 或镜像。
- 认证接口限流：登录 5 次/分钟/IP，注册 3 次/小时/IP，重置密码 3 次/小时/邮箱。
- Webhook 端点只接受签名有效的请求；签名比较使用常量时间函数。
- 跨租户访问一律返回 404，不泄露资源存在性。
- 密码与 API Key 只存哈希。

### 11.2 一致性

- 余额变更与账本写入必须在**同一数据库事务**内。
- 所有对外部（Creem）与内部（ARQ）的入口都必须幂等。
- 日终对账任务校验 §7.3 的不变式，偏差立即告警。

### 11.3 性能

- 估价接口 P95 < 50 ms（纯内存查表）。
- 冻结事务 P95 < 100 ms（不得因计费引入明显的提交延迟）。
- 账本表按 `(user_id, created_at DESC)` 建索引，明细分页 P95 < 200 ms。

### 11.4 可观测

- 关键指标：冻结/扣减/释放速率、释放占比（过高说明上游不稳）、超时兜底释放次数（应恒为 0）、webhook 失败率、对账偏差、每租户毛利。
- 告警：签名校验失败突增、`RELEASE_TIMEOUT` 出现、对账不一致、单用户小时消费异常。

---

## 12. 迁移方案

### 12.1 SQLite → PostgreSQL（前置，必须最先做）

这是整个 PRD 的技术前置。计费账本要求「余额变更与账本写入在同一事务内、并发下不双花」，SQLite 给不了这个保证。

**现状盘点（代码级）**

| 项 | 现状 | 位置 |
|----|------|------|
| 引擎 | `sqlite+aiosqlite`，因 aiosqlite 特性强制 `NullPool` | `backend/app/db.py:9-17`；`backend/app/config.py:23` |
| 迁移工具 | **完全没有 Alembic**。建表靠 `Base.metadata.create_all()` | `backend/app/db.py:init_db()` |
| 增量改列 | 手写的幂等 `_ensure_columns()`，**用 SQLite 专有的 `PRAGMA table_info`** 探测列 | `backend/app/db.py:_ensure_columns()` |
| DDL 方言 | `BOOLEAN NOT NULL DEFAULT 0`、`DROP COLUMN` 等写法为 SQLite 语法 | 同上 |
| 依赖 | 只有 `sqlalchemy>=2.0` + `aiosqlite>=0.19`，无 `asyncpg`、无 `alembic` | `backend/pyproject.toml:8-9` |
| 部署 | compose 无 postgres 服务；3 个服务硬编码 `DATABASE_URL: sqlite+aiosqlite:////app/data/dev.db` | `deploy/docker-compose.dev.yml:44,96,140` |

> **关键风险**：`_ensure_columns()` 一旦连上 PostgreSQL 会**当场报错**——`PRAGMA` 不是合法的 Postgres 语句。它不是"可以先凑合"的兼容问题，是硬阻断。

**迁移步骤**

1. 依赖加 `asyncpg` 与 `alembic`（`uv sync --project backend`）。
2. 引入 Alembic，以当前 ORM 模型生成 **initial revision**，作为基线。
3. **删除 `_ensure_columns()`**，其历史意图全部由 Alembic revision 承接。同时从 `init_db()` 移除 `create_all()`，改为启动时校验 migration head 是否最新。
4. `db.py` 去掉 SQLite 的 `NullPool` 分支，PostgreSQL 用默认 `QueuePool`，池大小按 backend + worker + vc-worker + mcp 四类进程分别配置。
   > 注意 SSE 长连接曾经打爆过 SQLite 连接池（`db.py` 注释有记录）。迁到 PG 后仍需确认 SSE 不长期持有 session——现有代码已经是"快照后立即释放 session"的写法，行为正确，迁移后回归验证即可。
5. compose 增加 `postgres:16-alpine` 服务与 `app-pgdata` 具名卷；`DATABASE_URL` 改为 `postgresql+asyncpg://...` 并集中到一处引用，避免 3 处硬编码漂移。密码走 `CLAUDE.md` 的 secrets 流程。
6. 数据搬迁：现有 SQLite 是**开发数据**，用一次性脚本按表全量搬（`projects` → `shots` → `reference_images` → `image_candidates` → `events` → `content_analyses` → `reference_samples`，按外键顺序）。搬完逐表比对行数。
7. 回归：跑完整后端测试套件 + 一次真实项目端到端。

**验收**：`_ensure_columns()` 已删除；`alembic upgrade head` 可在空库上一键建全表；全部测试在 PostgreSQL 上通过；SSE 并发 20 路不耗尽连接池。

**回滚**：保留 SQLite 库文件不删。`DATABASE_URL` 切回即可回退，因为第 0 期不改任何业务逻辑与表结构语义。

### 12.2 单租户 → 多租户数据回填

现有数据是内部单租户数据，迁移可以简单处理：

1. 用 Alembic revision 建 §7 全部新表（§12.1 完成后，所有表结构变更一律走 Alembic）。
2. 创建一个 `legacy@internal` 系统用户。
3. `Project.owner_id` 先建为 nullable，回填：按 `creator_name` 去重创建用户（无邮箱、状态 `restricted`，需管理员激活），把项目挂过去；无法归属的挂到 `legacy@internal`。
4. `ContentAnalysis.owner_id` 同样先建为 nullable。**该表没有任何归属线索**，存量数据一律挂到 `legacy@internal`，由管理员事后认领。
5. 两处回填完成后改为 `NOT NULL`。
6. 给所有迁移用户的账户初始化余额 0（历史消耗不追溯）。
7. 用一次性脚本对每个 `project_prefix` 调 `object_store.list_prefix()` 汇总对象大小，初始化 `storage_bytes_used`。

**回滚**：新表与新列均为增量，回滚只需关闭认证中间件，旧代码路径不受影响。

---

## 13. 分期交付

按「换地基 → 先堵漏 → 再计量 → 后收钱」排序。每期独立可上线。

### 第 0 期：迁移到 PostgreSQL（前置，不含任何业务变更）
- 加 `asyncpg` + `alembic` 依赖
- 引入 Alembic 并生成 initial revision
- **删除 `_ensure_columns()`**（含 SQLite 专有的 `PRAGMA`），移除 `create_all()`
- compose 加 `postgres:16-alpine` + `app-pgdata` 卷，`DATABASE_URL` 收敛到一处
- 连接池从 `NullPool` 改为 `QueuePool`
- 开发数据按外键顺序全量搬迁并比对行数

**验收**：`alembic upgrade head` 可在空库一键建全表；后端测试套件在 PostgreSQL 上全绿；SSE 并发 20 路不耗尽连接池。详见 §12.1。

> 这一期**不改任何业务逻辑**，因此可以独立上线并独立回滚（切回 `DATABASE_URL` 即可）。

### 第 1 期：堵住数据泄露（最高优先，不依赖计费）
- `users` 表 + 邮箱密码登录 + Cookie 会话（F1.1–F1.5）
- `Project.owner_id` + `ContentAnalysis.owner_id`（F2.1、F2.2）
- 全部 8 个 router 加认证与归属校验（F2.3、F2.5）
- 素材签名 URL 补归属校验 + TTL 收紧到 600 秒（F2.4）
- API Key 打通 MCP（F1.6）
- 数据回填（§12.2）

**验收**：以 A 账号登录后，用任何手段（API、SSE、素材 URL、MCP）都无法读到 B 账号的项目、内容分析、素材或事件流。

### 第 2 期：把钱算清楚（只计量，不收费）
- `usage_records` 表
- **只在 2 处埋点**：`video_generator.py`（视频生成）与 `image_generation.py`（图像生成）
- 定价表与版本机制（F5）
- 管理后台毛利报表（F11.3）

**验收**：跑完一个真实项目，账本能还原出每一次视频与图像生成调用及其成本，与 Vertex 账单误差 < 5%。

### 第 3 期：点数账本与拦截
- `credit_accounts` + `credit_transactions` + 两阶段扣费（F4）
- 估价与 402 拦截（F6）
- 前端余额展示、消费明细（F8）
- 超时兜底与日终对账（F4.5、§11.2）

**验收**：余额不足时任务被拦在入队前；上游失败后点数全额退回；并发双击只扣一次。

### 第 4 期：Creem 充值
- 点数包配置与 Creem product 对应（F7.1）
- 结账下单 + webhook 入账 + 幂等（F7.2–F7.6）
- 退款/拒付冲正（F7.4）
- 订单兜底对账（F7.7）

**验收**：测试环境（`test-api.creem.io`）完成一次真实支付并到账；同一 webhook 重投 5 次余额只增加一次；退款后余额正确冲正。

### 第 5 期：配额与公平性
- 并发配额与轮转调度（F9）
- 存储配额与级联清理（F10）
- 管理后台其余功能（F11）

---

## 14. 风险与未决问题

### 14.1 已拍板的决策

| # | 决策 | 依据 |
|---|------|------|
| **D1** | **迁移到 PostgreSQL**，作为第 0 期前置交付。 | SQLite 没有真正的行级锁，`SELECT ... FOR UPDATE` 在其上是空操作，并发扣费会双花，§7.3 的不变式无法保证。方案见 §12.1。 |
| **D2** | **v1 只对视频生成与图像生成计费**，文本类 LLM 成本忽略不计。 | 文本成本相对视频生成量级差约 1000 倍。这把计费接入面从 8 处压缩到 2 处。 |
| **D3** | 音色转换与内容分析转写**不计费**。 | 两者均为自建算力：CosyVoice 跑自有 GPU，转写用容器内的 faster-whisper（compose 有 `whisper-cache` 卷与 `asr` 依赖组），没有按次上游账单。 |

### 14.2 未决问题

| # | 问题 | 说明 | 建议 |
|---|------|------|------|
| Q1 | 上游单价未经官方校准 | §10 的成本数字来自公开资料，非官方账单。 | 实施前用 Vertex AI 官方定价页 + 一个月真实账单反推校准。 |
| Q2 | Creem 是否支持人民币结算 | 定价表以 ¥ 计，需确认 Creem 的币种支持与实际到账汇率。 | 与 Creem 确认；若仅支持美元，定价表改以 $ 为锚。 |
| Q3 | 新手赠送的滥用 | 邮箱验证挡不住批量注册。 | 第 1 期先只做邮箱验证；若观察到滥用，再加手机号或设备指纹。 |
| Q4 | 失败归因的准确性 | F6.5 要求 worker 显式给出归因，但现有 `_mark_shot_failed()` 只记文本消息。 | 需在第 3 期重构失败路径，引入结构化的 `failure_reason` 枚举。 |
| Q5 | 已生成素材的历史存储占用 | 迁移时不追溯历史消耗，但老项目会持续占用配额。 | 给迁移用户一次性提高配额，或设定清理宽限期。 |
| Q6 | 签名 URL 无法吊销 | COS 预签名 URL 一旦签发，在 TTL 内无法作废。用户删除项目后，已签发的 URL 仍可访问。 | F2.4 收紧 TTL 到 600 秒是主要缓解手段；若需强吊销，得改为后端中转流量，代价是放弃当前"不中转"的架构优势。 |
| Q7 | 自建 GPU 成本如何进毛利表 | D3 决定不向用户收费，但内部毛利报表仍需体现这部分成本。 | 按「GPU 月成本 ÷ 月均调用量」摊到 `upstream_cost_usd`，季度复算。不影响用户侧计费。 |

---

## 15. 验收标准（总体）

0. **地基**：全部后端测试在 PostgreSQL 上通过；`alembic upgrade head` 可在空库一键建全表；`_ensure_columns()` 已删除。
1. **隔离**：任意两个账号之间，通过 API、SSE、素材签名 URL、MCP 任一路径都无法互相读取数据（含项目与内容分析两类实体）。渗透式验证，不靠代码走读。
2. **计量完整**：一个完整项目跑完后，每一次**视频生成与图像生成**调用都有对应的 `usage_records`，无遗漏、无重复。其余操作不应产生 `usage_records`。
3. **账目自洽**：`balance + held == SUM(transactions.amount)` 在任意时刻成立。
4. **不为失败买单**：注入上游故障，点数全额退回。
5. **幂等**：webhook 重投、任务重试、用户双击，均不产生重复扣费或重复入账。
6. **拦截有效**：余额为 0 时，视频与图像生成一律返回 402，且不产生任何上游调用；其余操作不受余额影响，正常可用。

---

## 附录 A：参考资料

- [Creem Webhooks 文档](https://docs.creem.io/code/webhooks)
- [Creem Create Checkout Session API](https://docs.creem.io/api-reference/endpoint/create-checkout)
- [Creem 完整文档（llms-full.txt）](https://docs.creem.io/llms-full.txt)
- Veo 3.1 Fast 定价（第三方汇总，**待官方校准**）：[Veo 3.1 Pricing 2026](https://www.veo3gen.app/blog/veo-3-1-pricing-plans)、[Google Veo 成本指南](https://costgoat.com/pricing/google-veo)
