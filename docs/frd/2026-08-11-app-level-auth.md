# FRD：应用级登录鉴权

| | |
|---|---|
| 状态 | 待评审 |
| 日期 | 2026-08-11 |
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

线上库里有 **72 个项目 / 139 个分镜**，`creator_name` 全部是 `anonymous`
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
- 存量 72 个项目在登录后仍然可见（C-6）

### 非目标（本期明确不做）

- 自助注册、邮箱验证、找回密码 —— 账号由运维预置
- 角色/权限模型（RBAC）—— 本期所有登录用户权限相同
- OAuth / SSO / 第三方登录
- 多租户数据隔离 —— 本期登录用户共享同一份数据（见 FR-8 的决策点）
- 登录失败次数限制、验证码、审计日志

---

## 4. 功能需求

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

### FR-8　存量数据归属

登录后**必须仍能看到那 72 个项目**。两种做法，需选定（见 §7 决策点）：

- **方案 A（推荐）**：本期不做数据隔离 —— 所有登录用户看到全部项目，
  `creator_name` 仅作展示。存量数据零改动，风险最低。
- 方案 B：把 `creator_name='anonymous'` 的存量数据批量改到某个账号名下，
  并按身份过滤列表。需要数据迁移脚本 + 回滚预案。

---

## 5. 配置与密钥

| 项 | 位置 | 说明 |
|---|---|---|
| 会话有效期 | `config.yml` | 非敏感 |
| 会话存储 | 复用集群内已有的 redis | 无需新组件 |
| 账号与密码哈希 | secret | **不入库、不进 config.yml** |
| 机器令牌 | secret | 同上 |

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

### AC-2　登录后可用

登录取得 cookie 后，`/api/projects` 返回 `total: 72`（即存量数据仍可见，AC 覆盖 C-6）。

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

## 7. 待决策

1. **数据隔离**：FR-8 选方案 A（不隔离，推荐）还是 B（按用户隔离）？
2. **账号规模**：单账号够用，还是需要几个独立账号？
3. **会话有效期**：建议 7 天滑动过期；是否需要"记住我"？
4. **上线顺序**：鉴权上线会让**未登录的现有页面全部 401**。是否需要先部署
   后端（保持放行）、前端登录页就绪后再开启强制校验？

---

## 8. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| cookie 属性写错 | 登录后立刻掉线 | AC-4 专门覆盖；灰度时先在 preview 域验证 |
| 漏挂鉴权依赖 | 端点裸奔 | FR-3 默认拒绝 + AC-6 回归 |
| SSE 未带凭据 | 进度流静默失效，表现为"卡住" | AC-3 真实浏览器验证 |
| MCP 被一并挡死 | 对话式改分镜功能不可用 | FR-5 与 FR-3 同期上线，不可后置 |
| 存量数据看不见 | 用户以为数据丢了 | AC-2 断言 `total: 72` |
