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
- 角色/权限模型（RBAC）—— 本期所有登录用户权限相同
- OAuth / SSO / 第三方登录
- 登录失败次数限制、验证码、审计日志
- **按用户配额 / 速率限制** —— 但注意：若 FR-0 选开放自助注册，
  这一条就**必须**升级为本期需求，否则鉴权挡不住计费滥用

> 注：数据隔离**是**本期目标（FR-8，已定按用户过滤）；
> 自助注册是否开放尚未定（FR-0）。

---

## 4. 功能需求

### FR-0　账号从哪来（须先明确，见 §7）

账号规模定为 ≤ 1000，这已经不是"运维预置几个"的量级，必须明确开户方式：

| 方式 | 计费风险 |
|---|---|
| **邀请/审批制**（推荐） | 可控 —— 只有你放进来的人能用 |
| 开放自助注册 | ⚠️ **鉴权的初衷落空** |

> ⚠️ **登录不等于防滥用。** 做鉴权的原始动机是"别人能烧我的 Veo/Gemini 配额"。
> 如果注册是开放的，攻击者注册一个账号即可继续烧 —— 只是从"匿名烧"变成
> "注册后烧"，账单没有任何区别。
>
> 因此：**若选开放注册，必须同期引入按用户的配额或速率限制**（例如每人每日
> 可触发的生成次数上限），否则本 FRD 解决不了它要解决的问题。配额本期未列入
> 需求，需要的话应作为独立条目补入。

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

### FR-6　前端登录流程

- 新增登录页；未认证时访问任何页面重定向至此
- `api.ts` 的 `request()` 全部请求加 `credentials: 'include'`
  （当前未设置，跨站 cookie 不会被携带）
- 收到 `401` 时清理本地状态并跳转登录页
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

**FR-8.3 存量数据迁移**：线上存量项目的 `creator_name`
均为 `anonymous`，须归到一个指定账号名下，否则登录后列表为空。

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

---

## 4.1 分期上线（已定）

鉴权一旦强制，未登录的现有页面会全部 401。分三步，每步可独立回滚：

| 阶段 | 内容 | 开关 | 完成判据 |
|---|---|---|---|
| **P1** | 后端上线：`users` 表、登录/登出、会话校验依赖、机器令牌；**强制校验默认关闭** | `AUTH_ENFORCED=false` | 线上行为与现在完全一致；能用 curl 登录拿到 cookie |
| **P2** | 前端上线：登录页、`credentials: 'include'`、SSE `withCredentials`、401 跳转 | 仍为 `false` | 能登录、能看到数据；关闭仍不影响未登录访问 |
| **P3** | 数据迁移 + 打开强制校验 | `AUTH_ENFORCED=true` | AC-1～AC-6 全通过 |

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

> **账号为什么必须进数据库、而不是 secret。** 账号规模定为 ≤ 1000，
> 这个量级下把用户名/密码哈希塞进 secret 已不可行：改一个用户要重新
> 渲染 secret 并重启 Pod，且无法承载"注册时间、状态、归属项目"这类字段。
> 因此新增 `users` 表（`id` / `username` 唯一 / `password_hash` / `created_at` /
> `is_active`）。secret 只保留**机器令牌**一项。

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

### AC-7　不引入回归

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

### 仍待明确

1. **开户方式（FR-0）**：邀请/审批制，还是开放自助注册？
   若开放注册，**必须同期加按用户配额**，否则鉴权解决不了计费问题。
2. **存量项目归到哪个账号名下？** 需要一个具体用户名，迁移脚本要用。
3. **存量迁移走哪条路？** 复用现有 `creator_name`（省事）还是新增
   `owner_id` 外键（规范，需手写 ALTER）。见 FR-8.3。

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
| **开放注册后配额仍被烧** | 鉴权做完但账单照旧 | FR-0 已标注；选开放注册则配额为同期必做项 |
| `create_all` 不会 ALTER 已有表 | 以为加了列其实没加，运行时报错 | FR-8.3 已注明；加列必须手写迁移并验证 |
