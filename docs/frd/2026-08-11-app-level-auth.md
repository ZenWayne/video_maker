# FRD：应用级登录鉴权

| | |
|---|---|
| 状态 | 决策已定，待实现 |
| 日期 | 2026-08-11 |
| 已定决策 | 按用户过滤数据；账号规模 ≤ 1000；会话 7 天滑动过期；分期上线 |
| 范围 | `backend/app`（新增会话鉴权）、`frontend-vite`（登录页 + 凭据透传）、`deploy/k8s`（配置与密钥） |

---

## 1. 背景与问题

线上现状（已实测确认，非推测）：

```bash
$ curl -s -o /dev/null -w '%{http_code}' https://video-maker-api.kuanzw.com/api/projects
200
```

不带任何凭据、不带 `Origin` 头，直接返回全部项目数据。具体地：

- **没有任何鉴权层。** `backend/app/main.py` 只挂了 `CORSMiddleware`，没有认证中间件或依赖。
- **身份是自称的。** `backend/app/api/projects.py:101` 的 `_require_user` 只校验
  `X-User-Name` 请求头**存在**，不校验内容；任何人可以自称任何身份。而且它
  全项目**只用在 1 个端点**上（`grep -c "Depends(_require_user)"` → 1）。
- **没有数据隔离。** `list_projects` 的 `creator` 是可选查询参数，不从身份推导，
  所以任何调用方都能看到并操作全部项目。
- **CORS 保护不了这件事。** 它只约束浏览器；`curl` 和脚本完全无视。

### 为什么现在必须做

API 上挂着**会产生真实费用**的端点 —— `POST /api/projects/{id}/start`、
`/regenerate-shots`、`/generate-tail-frame`、`/voice-convert` 等会触发
Vertex AI（Veo / Gemini）调用。域名在公网可解析，被扫描到只是时间问题，
届时是直接烧配额。

---

## 2. 硬性约束（这些决定了方案形态，不是偏好）

### C-1　SSE 决定了凭据只能放 cookie

`frontend-vite/src/lib/sse.ts:14` 和 `analysisSse.ts:25` 都是裸的
`new EventSource(url)`。**EventSource 不支持自定义请求头** —— 这是 Web 标准
限制，不是实现问题。所以 `Authorization: Bearer` 这类方案**无法覆盖 SSE**，
而 SSE 是生成进度流的唯一通道。

可行的只有两条：

| 方式 | 评价 |
|---|---|
| **httpOnly cookie + `EventSource(url, { withCredentials: true })`** | ✅ 采用。浏览器自动携带，JS 读不到，可防 XSS 窃取 |
| token 放 query string | ❌ 会进访问日志、Referer、浏览器历史 |

### C-2　跨站 cookie 的属性是被架构定死的

前端在 Vercel（`https://video-maker.kuanzw.com`），API 在集群
（`https://video-maker-api.kuanzw.com`）—— **不同站点**。因此会话 cookie 必须：

```
Set-Cookie: session=<...>; HttpOnly; Secure; SameSite=None; Path=/
```

`SameSite=Lax`（默认值）在跨站请求上**不会发送**，会导致登录后立刻表现为未登录。

### C-3　cookie 不能设 `Domain=.kuanzw.com`

必须是 host-only（不写 `Domain` 属性），只作用于 `video-maker-api.kuanzw.com`。

原因：`kuanzw.com` 这个 zone 上还跑着 xray 的 CDN 路由
（`kube-system/xray-routes` IngressRoute，`Host(kuanzw.com)` 匹配
`/none`、`/hbproxy` 等路径）。设成 domain-wide 会把会话 cookie 发送到这些
无关主机上，属于凭据泄漏。

### C-4　CORS 必须放行凭据且不能用通配符

`backend/app/main.py:94-99` 已经是 `allow_credentials=True`，这是必需的
（否则浏览器丢弃跨站 cookie）。**代价是 `allow_origins` 不能用 `*`** ——
规范禁止二者共存。当前 `CORS_ORIGINS` 已按精确来源配置，需保持。

> 已知坑（见 `deploy/k8s/README.md`）：`cors_origins.split(",")` 不做 trim，
> 逗号后带空格会静默失配。

### C-5　存在非浏览器调用方

`backend/mcp_server/server.py:277` 通过 `BACKEND_BASE_URL` 直接调后端。
它是机器客户端，**没有浏览器、不存 cookie、无法交互登录**，因此必须有一条
独立的机器凭据通道，不能只做会话 cookie。

### C-6　存量数据的归属

线上库里有存量项目（2026-08-07 迁入时 72 个 / 139 个分镜，之后持续新增，
8-11 已达 74），`creator_name` 全部是 `anonymous`
（这批数据是 2026-08-07 从本地开发库迁入的）。引入真实账号后必须明确这些
数据归谁，否则登录之后用户会看到空列表 —— 与 2026-08-10 那次"项目是空的"
是同一类问题。

---

## 3. 目标与非目标

### 目标

- 未认证请求无法访问任何业务数据或触发任何计费操作
- 覆盖 **REST 与 SSE 两条通道**（C-1）
- 机器客户端（MCP）有独立可用的凭据通道（C-5）
- 前端有可用的登录/登出流程，会话过期能自动引导重新登录
- 存量项目在登录后仍然可见（C-6）

### 非目标（本期明确不做）

- 邮箱验证、找回密码
- 完整的角色/权限模型（RBAC）—— 只有 `is_admin` **一个布尔位**（FR-9.4）
- OAuth / SSO / 第三方登录
- 登录失败次数限制、验证码、审计日志
- 点数的**充值/支付**链路 —— 本期只有管理员手工发放（FR-9.4）
- 防批量注册刷体验额度（见 §8 残余风险）

> 注：数据隔离（FR-8）与点数系统（FR-9）**都是**本期目标。
> 开放自助注册已定（FR-0），因此 FR-9 不可裁剪 —— 它是唯一的计费闸门。

---

## 4. 功能需求

### FR-0　开放自助注册（已定）

`POST /api/auth/register`，任何人可自助注册。

- 用户名唯一；密码哈希存储（同 FR-1）
- 注册成功即赠送**体验点数**（额度见 §5 配置，可调）
- 注册本身不需要邀请码、不需要审批

> ⚠️ **开放注册意味着鉴权本身挡不住计费滥用** —— 攻击者注册一个账号即可
> 继续烧 Veo/Gemini 配额。因此 **FR-9 点数系统是本期的必做项，不是可选项**：
> 它才是真正的计费闸门，鉴权只负责把消费挂到具体的人头上。
>
> 残余风险：批量注册刷体验额度（详见 §8 风险表）。

### FR-1　登录

`POST /api/auth/login`，请求体 `{ username, password }`。

- 校验通过：签发会话，通过 `Set-Cookie` 下发（属性见 C-2、C-3），
  响应体返回 `{ username }`
- 校验失败：`401`，响应体**不得**区分"用户不存在"与"密码错误"
- 密码在服务端以哈希形式存储，**禁止明文**；算法用 `bcrypt` 或 `argon2`

### FR-2　登出

`POST /api/auth/logout`：服务端使该会话失效，并下发过期的 `Set-Cookie` 清除浏览器侧。

### FR-3　会话校验

新增 FastAPI 依赖（如 `require_session`），从 cookie 解析并校验会话：

- 有效 → 注入当前用户身份供下游使用
- 无效/缺失/过期 → `401`

**必须挂载到 `/api` 下的全部路由**，仅以下例外：

| 端点 | 原因 |
|---|---|
| `GET /health` | K8s 探针，且不在 `/api` 前缀下 |
| `POST /api/auth/login` | 登录本身 |
| `POST /api/auth/register` | 注册本身（FR-0 开放注册） |

> 白名单只有这三条，**且不含任何会触发计费的端点**。新增免鉴权端点需评审。

> ⚠️ 采用**默认拒绝**：新增路由若忘记加依赖应当是"进不去"，而不是"裸奔"。
> 建议用全局中间件或 `APIRouter(dependencies=[...])` 统一挂载，
> 而不是逐个端点加 `Depends` —— 后者正是 `_require_user` 只覆盖 1 个端点的成因。

### FR-4　SSE 鉴权

- 后端：`/api/projects/{id}/stream`、`/api/analyses/{id}/stream` 同样走 FR-3 校验
- 前端：`sse.ts` 与 `analysisSse.ts` 改为
  `new EventSource(url, { withCredentials: true })`

### FR-5　机器凭据

为 MCP 等非浏览器调用方提供**静态令牌**通道：

- 请求头 `Authorization: Bearer <token>`，与会话 cookie 二者满足其一即放行
- 令牌来自 secret（遵循仓库既有约定：`deploy/secrets.yml` → `secrets/<key>`），
  **不得写入 `config.yml` 或任何入库文件**
- MCP 服务端从环境变量读取并在 `BackendClient` 中携带

### FR-6　前端登录 / 注册流程

- 新增**登录页与注册页**；未认证时访问任何页面重定向至登录页
- `api.ts` 的 `request()` 全部请求加 `credentials: 'include'`
  （当前未设置，跨站 cookie 不会被携带）
- 收到 `401` 时清理本地状态并跳转登录页
- 收到 `402`（点数不足）时给出明确提示，**不要**跳登录页 ——
  两者都是"被拒绝"，但处置完全不同，混在一起会让用户莫名其妙被登出
- 顶部展示当前余额（数据来自 `GET /api/auth/me`），并在触发计费操作前
  就提示余额不足，而不是等后端 `402`
- 登出入口

### FR-7　身份来源改为会话

- 删除 `getUserName()` / `X-User-Name` 这条自称身份的链路
- 后端 `creator_name` 改由会话身份填充，`_require_user` 由 FR-3 的依赖取代

### FR-8　按用户过滤数据（已定）

每个用户只能看见并操作自己的项目。

**FR-8.1 列表过滤**：`list_projects` 按会话身份过滤，而不是按可选的
`creator` 查询参数。该参数应移除或忽略，否则等于把过滤条件交给调用方。

**FR-8.2 逐对象归属校验（关键，不可省）**：仅过滤列表**不够**。所有
project 作用域的端点 —— `GET/PATCH/DELETE /api/projects/{id}`、其下的
shots、trim、voice-convert、export、stream 等 —— 都必须校验该 project
属于当前会话用户，否则知道 `id` 就能读写他人数据（IDOR）。project id 是
UUID，但**不可当作访问控制**：它会出现在 URL、日志、分享链接里。

> 建议实现成一个统一依赖（如 `get_owned_project`），由它同时完成
> "取 project" 与 "校验归属"，让端点无法只取不校验。

**FR-8.3 存量数据迁移（已定）**：新建账号 **`stella`**，把线上存量项目
（`creator_name='anonymous'`）全部归到它名下；并新增 **`owner_id`** 外键作为
权威归属字段（不复用 `creator_name`）。`stella` 同时置为管理员（FR-9.4）。

迁移步骤（顺序不可颠倒）：

1. **备份** `/app/data/dev.db`（该 PVC 无任何备份机制，见 §8）
2. 记录基线：`select count(*) from projects`
3. 建 `users` 表并插入 `stella`（`create_all` 自动建表）
4. 建列 `projects.owner_id`（在 `_ensure_columns` 中按既有写法追加）
5. 回填：把 `creator_name='anonymous'` 的行的 `owner_id` 指向 `stella`
6. 校验：`select count(*) from projects where owner_id is null` 应为 **0**
7. 确认无误后才切换读路径、打开强制校验（P3）

> `creator_name` 保留作展示字段，但**访问控制一律以 `owner_id` 为准** ——
> 两个字段并存时必须明确谁是权威，否则会出现"改了显示名就越权"的漏洞。

> **建表/建列走仓库既有机制，不引入 alembic。** `backend/app/db.py` 启动时
> 依次调用 `Base.metadata.create_all`（只建缺失的**表**，不会 ALTER）和
> `_ensure_columns(conn)`（幂等建**列**：`PRAGMA table_info` 判断后
> `ALTER TABLE ... ADD COLUMN`）。所以：
> - `users` 表由 `create_all` 自动建出
> - 若新增 `projects.owner_id`，在 `_ensure_columns` 里按既有写法加一段即可，
>   **无需引入迁移框架**
>
> 两条路（见 §7 待明确 #3）：
> - **省事**：不加列，复用现有 `creator_name`，迁移即
>   `UPDATE projects SET creator_name='<账号>' WHERE creator_name='anonymous'`
> - **规范**：新增 `owner_id` 外键 + 索引，回填后再切换读路径
>
> ⚠️ **数据回填（UPDATE）不在上述机制覆盖范围内**，必须单独写脚本；
> 且迁移前必须备份 `/app/data/dev.db`（该 PVC 无任何备份机制，见 §8 风险）。

### FR-9　点数系统（已定，本期必做）

所有需要 LLM 的功能都要消耗点数；余额不足则拒绝，不得触发任何计费调用。

#### FR-9.1　哪些操作扣点

按实际调用的外部计费服务划分（已逐个核对代码，非推测）：

| 操作 | 入口 | 走什么 | 扣点 |
|---|---|---|---|
| 生成分镜视频 | `/start`、`/continue-generation`、`/confirm-tail-frame` → `run_shot_pipeline` | **Veo（最贵）** | ✅ 按分镜数 |
| 生成/重写剧本 | `/start`、`/regenerate-script` → `run_screenwriter` | Gemini | ✅ |
| 重新生成分镜表 | `/regenerate-shots` | Gemini | ✅ |
| 生成首帧/尾帧 | `/generate-first-frame`、`/generate-tail-frame` → `run_image_candidate` | Gemini 图像 | ✅ |
| 内容分析 | `POST /api/analyses` → `run_content_analysis` | Vertex 简报 | ✅ |
| AI 改写 | `/ai-edit`、`/ai-edit-prompt`、`/rewrite-prompt` → `app.agents.shot_editor` | 同步 LLM | ✅ |
| **导出合并** | `/export` → `run_merger` | ffmpeg，本地 | ❌ 不扣 |
| **音色转换** | `/voice-convert*` → `arq:vc` 队列 | 本地 ONNX（vc2） | ❌ 不扣 |
| 项目 CRUD、reset、cancel、裁剪、抽帧 | — | 无外部调用 | ❌ 不扣 |

> 单价按操作类型分别配置（§5），因为成本差着数量级 —— 一条 Veo 视频远贵于
> 一次 Gemini 文本调用，用统一单价会让某一侧严重失真。

#### FR-9.2　扣费时机：入队前预扣，失败退款

**必须在入队前扣**，不能"成功后再扣"。原因是并发：若先入队后扣，用户可以
同时发起 N 个任务，每个都通过余额检查，最终透支。

```
请求 → 校验余额 → 原子扣减 + 写流水(reserve) → 入队
                ↓ 余额不足
              402，不入队、不产生任何调用
```

**退款**：worker 任务失败时，按流水记录退回对应点数（写一条 refund 流水）。
`run_shot_pipeline` 是多分镜任务，**按分镜粒度退**：失败几个退几个，
不能因为一个分镜失败就整单退或整单不退。

> 现实中 Veo 确实会失败（`kie_max_retries`、"upstream gen failed" 等在配置里
> 都有对应项），所以退款不是边缘情况，是常规路径。

#### FR-9.3　流水表

新增 `credit_ledger`：`id` / `user_id` / `delta`（正负）/ `reason`
（`register` / `grant` / `reserve` / `refund`）/ `ref_type` / `ref_id` /
`created_at`。

用户当前余额以 `users.credits` 为准，流水用于审计与退款依据。
**扣减与写流水必须在同一事务内**，否则会出现扣了钱查不到出处、或退款重复执行。

#### FR-9.4　发放接口（管理员）

`POST /api/admin/users/{username}/credits`，请求体 `{ delta, reason }`。

- 仅 `is_admin` 用户可调用，其余一律 `403`
- `delta` 可正可负（正=发放，负=回收）
- 每次调用写一条 `grant` 流水

> 这引入了**一个布尔位** `users.is_admin` —— 不是完整 RBAC（仍是非目标）。
> `stella`（FR-8.3 承接存量数据的账号）应置为管理员。

#### FR-9.5　余额可见

`GET /api/auth/me` 返回当前用户名与余额，供前端展示。
余额不足时前端应在触发前给出提示，而不是等 `402` 才报错。

---

## 4.1 分期上线（已定）

鉴权一旦强制，未登录的现有页面会全部 401。分三步，每步可独立回滚：

| 阶段 | 内容 | 开关 | 完成判据 |
|---|---|---|---|
| **P1** | 后端上线：`users` / `credit_ledger` 表、注册/登录/登出、会话校验依赖、机器令牌、点数扣减与退款、管理员发放接口；**强制校验默认关闭** | `AUTH_ENFORCED=false` | 线上行为与现在完全一致；能用 curl 注册/登录拿到 cookie；能发放点数 |
| **P2** | 前端上线：注册/登录页、余额展示、`credentials: 'include'`、SSE `withCredentials`、401/402 处理 | 仍为 `false` | 能登录、能看到数据；关闭仍不影响未登录访问 |
| **P3** | 建 `stella` 账号 + 数据迁移（FR-8.3 七步）+ 打开强制校验 | `AUTH_ENFORCED=true` | AC-1～AC-9 全通过 |

> 开关只控制"未认证是否放行"，**不控制**是否签发会话 —— 否则 P1/P2 无法验证。
>
> P3 之前必须完成 FR-8.3 的数据迁移并备份数据库，顺序不能颠倒：
> 先开强制校验再迁移，中间窗口里用户会看到空列表。
>
> 回滚：把 `AUTH_ENFORCED` 改回 `false` 即可恢复放行，无需回滚镜像。

## 5. 配置与密钥

| 项 | 位置 | 说明 |
|---|---|---|
| 会话有效期 | `config.yml` | 非敏感；定为 **7 天滑动过期** |
| 会话存储 | 复用集群内已有的 redis | 无需新组件 |
| **账号与密码哈希** | **数据库 `users` 表** | 见下方说明 |
| 机器令牌 | secret | 遵循既有 secret 约定 |

> **账号为什么必须进数据库、而不是 secret。** 开放自助注册 + 规模 ≤ 1000，
> 这个量级下把用户名/密码哈希塞进 secret 完全不可行（注册一个用户就要重新
> 渲染 secret 并重启 Pod）。因此新增 `users` 表：
> `id` / `username` 唯一 / `password_hash` / `credits` / `is_admin` /
> `is_active` / `created_at`。secret 只保留**机器令牌**一项。

点数相关配置（`config.yml`，非敏感、可不改代码调整）：

| 键 | 含义 |
|---|---|
| `CREDITS_ON_REGISTER` | 注册赠送的体验点数 |
| `CREDIT_COST_VIDEO_SHOT` | 每个分镜视频（Veo）单价 |
| `CREDIT_COST_SCRIPT` | 剧本生成 / 重写 |
| `CREDIT_COST_SHOTLIST` | 分镜表重新生成 |
| `CREDIT_COST_IMAGE` | 首帧 / 尾帧图像 |
| `CREDIT_COST_ANALYSIS` | 内容分析 |
| `CREDIT_COST_AI_EDIT` | AI 改写类同步调用 |

> 单价放 `config.yml` 而非硬编码：Veo 与 Gemini 的实际成本会变，
> 调价不该需要改代码重新构建镜像。

> 遵循仓库既有 secret 约定（`CLAUDE.md`）：新增键要同时补进
> `deploy/secrets.yml.example`（占位值）。
>
> ⚠️ `deploy/k8s/README.md` 已记录过一个雷：占位符模板若放在
> `deploy/k8s/` 目录内且 `metadata.name` 与线上一致，一次
> `kubectl apply -f deploy/k8s/` 就会用占位值覆盖线上真实密钥。
> 本期新增密钥必须沿用 `examples/` 子目录的隔离做法。

---

## 6. 验收标准

按仓库既有的验证文化 —— **断言实际结果，不接受代理信号**。

### AC-1　未认证一律拒绝

```bash
curl -s -o /dev/null -w '%{http_code}' https://video-maker-api.kuanzw.com/api/projects
# 期望 401（当前是 200）
```

对每一个 `/api` 端点抽样验证，含 SSE 与所有计费端点。

### AC-2　登录后可用，且存量数据没丢

用承接存量数据的那个账号登录后，`/api/projects` 的 `total` 应等于
**迁移前实测的项目数 N**（覆盖 C-6 与 FR-8.3；若为 0 说明迁移没做或做错）。

> ⚠️ **N 必须在迁移当天现取，不要照抄文档里的数字。** 项目在持续新增：
> 2026-08-07 迁入时是 72，2026-08-11 已是 74。执行迁移前先跑
> `select count(*) from projects` 记下基线，迁移后逐一比对。

### AC-2.1　跨用户不可见（FR-8.1）

用另一个账号登录，`/api/projects` 返回 `total: 0` —— 看不到他人项目。

### AC-2.2　知道 id 也拿不到（FR-8.2，防 IDOR）

以账号 B 的会话直接请求账号 A 的项目，逐个端点验证均为 `403`/`404`：

```
GET    /api/projects/{A的id}
PATCH  /api/projects/{A的id}
DELETE /api/projects/{A的id}
GET    /api/projects/{A的id}/stream
POST   /api/projects/{A的id}/start
```

> 这条**必须逐端点跑**，不能只测列表。只做列表过滤而漏掉归属校验，
> 是这类改造最典型的漏洞 —— 列表看着干净，换个 URL 就全拿到了。

### AC-3　SSE 通道同样受保护且可用

- 不带 cookie 连 `/api/projects/{id}/stream` → 401
- 带 cookie 且前端设了 `withCredentials: true` → 能持续收到事件

> 这条**必须在真实浏览器里验证**：`EventSource` 的 `withCredentials`
> 行为无法用 curl 等价复现。

### AC-4　跨站 cookie 真的生效

在 `https://video-maker.kuanzw.com` 登录后刷新页面仍是登录态。
这条用于兜住 C-2 —— 若 cookie 属性写错（如漏了 `SameSite=None`），
表现就是"登录成功但一刷新就掉线"。

### AC-5　机器令牌可用且独立

MCP 携带 `Authorization: Bearer` 调通；令牌错误时 401。

### AC-6　默认拒绝可回归

新增一个不加鉴权依赖的临时路由，验证它**依然**返回 401 —— 证明 FR-3
是全局兜底而非逐端点挂载。

### AC-7　余额不足时不产生任何计费调用（FR-9.2）

把某账号点数置 0，调用每一个 FR-9.1 中标 ✅ 的端点：

- HTTP `402`
- **arq 队列中没有新任务**（`redis` 侧核对，不能只看接口返回码）
- 后端日志中没有对应的 Vertex/Veo 调用

> 这条是整个点数系统的核心判据。只断言"返回 402"不够 —— 若代码是
> 先入队再扣费，接口照样可能返回 402 而任务已经在跑了。

### AC-8　并发不能透支（FR-9.2）

余额仅够 1 次生成时，并发发起 10 个请求：**恰好 1 个成功，9 个 402**，
且最终余额不为负。用于验证扣减是原子的。

### AC-9　失败要退款，且按分镜粒度（FR-9.2）

构造一个会失败的分镜生成（可在 worker 侧注入失败，不必真调 Veo）：

- 5 个分镜预扣 5 份，其中 2 个失败 → 最终净扣 3 份
- `credit_ledger` 中能查到 1 条 reserve、2 条 refund
- 重复触发退款不得重复退（幂等）

### AC-10　管理员发放（FR-9.4）

- `stella` 调 `POST /api/admin/users/{username}/credits` 成功，余额增加，流水可见
- 普通用户调用同一接口返回 `403`

### AC-11　不引入回归

`npx tsc --noEmit` 0 错误、`npm run build` 通过、`vitest` 单元测试全过、
后端测试全过。

---

## 7. 决策记录与遗留问题

### 已定（2026-08-11）

| # | 决策 | 影响 |
|---|---|---|
| 1 | **按用户过滤数据** | 触发 FR-8.1/8.2/8.3：列表过滤 + 逐对象归属校验 + 存量迁移 |
| 2 | **账号规模 ≤ 1000** | 用户必须进数据库（§5），不能放 secret；并引出 FR-0 |
| 3 | **会话 7 天滑动过期** | 无额外影响 |
| 4 | **分期上线** | 见 §4.1，`AUTH_ENFORCED` 开关三阶段 |

### 已定（2026-08-11 第二轮）

| # | 决策 | 影响 |
|---|---|---|
| 5 | **开放自助注册** | FR-0；使 FR-9 点数系统成为必做项 |
| 6 | **点数系统 + 管理员发放接口** | FR-9 全套：扣费、退款、流水、`is_admin` |
| 7 | **存量数据归到新账号 `stella`** | FR-8.3；`stella` 同时为管理员 |
| 8 | **新增 `owner_id` 外键** | FR-8.3；`creator_name` 降级为展示字段 |

### 仍待明确

1. **各操作的点数单价**（§5 的 `CREDIT_COST_*`）与**注册赠送额度**
   （`CREDITS_ON_REGISTER`）具体填多少？建议按外部实际成本折算后再定，
   本 FRD 只定"可配置"，不定数值。
2. **是否需要防批量注册**（同一 IP/设备重复注册刷体验额度）。本期列为
   非目标，但若发放额度较大，建议至少加一层注册频率限制。

---

## 8. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| cookie 属性写错 | 登录后立刻掉线 | AC-4 专门覆盖；灰度时先在 preview 域验证 |
| 漏挂鉴权依赖 | 端点裸奔 | FR-3 默认拒绝 + AC-6 回归 |
| SSE 未带凭据 | 进度流静默失效，表现为"卡住" | AC-3 真实浏览器验证 |
| MCP 被一并挡死 | 对话式改分镜功能不可用 | FR-5 与 FR-3 同期上线，不可后置 |
| 存量数据看不见 | 用户以为数据丢了 | AC-2 断言 `total` 等于迁移前实测基线 |
| **只做列表过滤、漏了归属校验** | 换个 URL 就能读写他人项目（IDOR） | FR-8.2 用统一依赖强制；AC-2.2 逐端点回归 |
| **迁移脚本写错，误改他人数据** | 数据错乱且**不可恢复** | 迁移前必须备份 `/app/data/dev.db` —— 该 PVC 是 `local-path` + `reclaimPolicy=Delete`，**无任何备份机制**；先在副本上演练 |
| **先入队后扣费** | 并发下透支，账单照烧 | FR-9.2 要求入队前原子扣减；AC-7 核对队列、AC-8 验并发 |
| **失败不退款** | 用户白扣点数，信任受损 | FR-9.2 按分镜粒度退款；AC-9 覆盖，含幂等 |
| **漏扣某个 LLM 入口** | 该入口成为免费通道 | FR-9.1 已逐个核对代码列全；新增 LLM 入口必须同步登记 |
| **批量注册刷体验额度** | 账单被稀释着烧 | ⚠️ **本期未解决**（非目标）。缓解：把 `CREDITS_ON_REGISTER` 设小；必要时加注册频率限制 |
| `creator_name` 与 `owner_id` 权威不清 | 改显示名即可越权 | FR-8.3 明确访问控制一律以 `owner_id` 为准 |
| `create_all` 不会 ALTER 已有表 | 以为加了列其实没加，运行时报错 | FR-8.3 已注明；加列必须手写迁移并验证 |
