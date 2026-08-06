# Video Maker 上云清单（k3s + Vercel + Cloudflare / demo）

**定位**：普通 demo。单副本、无 HA、不做备份、不做认证。

## 架构

```
浏览器
 │
 ├─ https://video.kuanzw.com ─────────────────────────► Vercel（静态 SPA，集群外）
 │
 ├─ https://api.video.kuanzw.com/api/*
 │        └─ Cloudflare 橙云 (Flexible) ─http─► 139.199.78.140:80
 │              └─ traefik (hostPort 80，钉在 vm-0-8-ubuntu)
 │                   └─ Ingress ─► Service backend:8000
 │
 └─ https://<bucket>.cos.ap-guangzhou.myqcloud.com/...  ◄── 302 签名 URL
          媒体直连 COS，不经 Cloudflare、不经 ingress

集群内只有 3 个 workload：backend / worker / redis，全部钉在 vm-0-8-ubuntu
```

**集群里没有 nginx**。静态托管在 Vercel，路由由 traefik Ingress 承担，媒体在腾讯云 COS。

---

## 0. 已实地核对的现状（2026-08-06）

### 集群

| 项 | 值 |
|---|---|
| 集群 | k3s v1.35.4+k3s1，3 节点，context `racknerd` |
| 目标节点 | `vm-0-8-ubuntu`，内网 `10.200.0.2`，公网 **`139.199.78.140`**（腾讯云 CVM） |
| 节点状态 | ⚠️ **`Ready,SchedulingDisabled`**（已 cordon），节点上 **0 个 pod** |
| 节点标签 | `kubernetes.io/hostname=vm-0-8-ubuntu`、`role=monitoring` |
| 默认 StorageClass | `local-path`（`WaitForFirstConsumer`，reclaimPolicy=**Delete**） |
| **Ingress 资源** | **全集群 0 个** —— 目前没有任何应用在用 ingress |
| IngressClass | 仅 `traefik`（k3s 自带，v3.6.13） |
| traefik 位置 | Deployment `replicas=1`，经 HelmChartConfig 用 nodeSelector 钉在 `cool-cube-1.localdomain` |

> **结论：把 traefik 挪到 `vm-0-8-ubuntu` 的爆炸半径是零** —— 全集群没有 Ingress 资源，
> 没有任何东西依赖它现在的位置。不需要另装一套 ingress controller。

### 80 端口在 `vm-0-8-ubuntu` 上是空的 ✅

k3s 的 `svclb-traefik` DaemonSet 抢的就是 hostPort 80，但它带
`nodeSelector: svccontroller.k3s.cattle.io/enablelb=true`，而 `vm-0-8-ubuntu` **没有这个标签**。

> ⚠️ **永远不要给 `vm-0-8-ubuntu` 打 `svccontroller.k3s.cattle.io/enablelb=true` 标签** ——
> 一打上 svclb 就来抢 hostPort 80，traefik pod 会 Pending。

### 代码现状（影响方案的两点）

- **媒体已全量迁到腾讯云 COS**（`feat(cos)!: 删除 /api/media 静态挂载，媒体改走签名 URL`）。
  `assets` 路由现在是 **302 重定向到 COS 预签名 URL**（`RedirectResponse(..., 302)`），
  仓库里还有「不写本地磁盘」守卫测试。→ **不再需要媒体 PVC**。
- **前端已支持绝对 API 地址**：`src/lib/api.ts` 与 `src/lib/sse.ts` 都是
  `const BASE = import.meta.env.VITE_API_BASE || ''`，54 个接口调用全部走 `request()`
  的 `` `${BASE}${path}` ``。→ **换跨域 API 零代码改动**，只需构建时给 `VITE_API_BASE`。

---

## 1. 上线前置条件

- [ ] **解除节点 cordon**（当前带 `node.kubernetes.io/unschedulable:NoSchedule` 污点，不解除全 Pending）
      ```bash
      kubectl uncordon vm-0-8-ubuntu
      kubectl get node vm-0-8-ubuntu     # 期望 Ready，无 SchedulingDisabled
      ```

- [ ] **腾讯云安全组放行 TCP 80**（入方向，源 `0.0.0.0/0`）。
      漏了的表现是「pod 全 Running 但外网连不上」。

- [ ] **确认节点本机 80 没被占用**
      ```bash
      ssh <node> "sudo ss -lntp | grep ':80 '"     # 期望无输出
      ```

- [ ] **确认节点能访问 Google API**。后端要调 Vertex AI（Veo / Gemini），节点在腾讯云国内，
      **直连必然失败**。需在节点上跑代理并给 backend/worker 注入 `HTTPS_PROXY`
      （dev 就是这么干的：`host.containers.internal:10809`）。
      > 这是唯一会让 demo「页面能开但生成不了」的硬伤，提前定方案。

- [ ] **确认 CVM 与 COS 桶同地域**。`config.yml` 里桶是 `ap-guangzhou`，
      同地域后端↔COS 才走内网、免流量费。

---

## 2. 镜像

**只需要 backend 一个镜像**（worker 复用同一镜像换 command）。前端在 Vercel，不再构建前端镜像。

节点在国内，ghcr.io 大概率拉不动 → 用 Docker Hub（集群已有先例：`tarot` ns 用
`docker.io/i6o6i/ai-tarot-backend:latest` + `imagePullSecrets: dockerhub-pull-secret`）。

- [ ] 构建并推送
      ```bash
      make build-backend REGISTRY=docker.io NAMESPACE=i6o6i TAG=demo
      make push          REGISTRY=docker.io NAMESPACE=i6o6i TAG=demo
      ```
- [ ] 复制拉取 secret 到新 namespace
      ```bash
      kubectl get secret dockerhub-pull-secret -n tarot -o yaml \
        | sed 's/namespace: tarot/namespace: video-maker/' \
        | kubectl apply -f -
      ```

### ⚠️ 镜像本身的坑（上线前必须确认）

- [ ] **`backend/Dockerfile` 的依赖是手写死的 pip 列表**，不是 `pyproject.toml`：
      ```
      fastapi uvicorn sqlalchemy aiosqlite arq redis pydantic pydantic-settings
      google-genai ffmpeg-python python-multipart sse-starlette python-json-logger
      ```
      对照 `backend/pyproject.toml`，这个列表**漏了 6 个依赖**：
      `cos-python-sdk-v5`（COS，现在的媒体主链路，缺了直接起不来）、
      `langfuse`（config 里 `LANGFUSE_ENABLED=true`）、
      `openai`、`httpx`、`scikit-image`、`fastmcp`。
      还有一个**装错了的**：Dockerfile 写 `ffmpeg-python`，pyproject 是 `python-ffmpeg` —— 两个不同的包。
      → **建议直接改成 `uv sync --project .`**，和 dev 保持一致；
      至少先本地 `podman run` 跑一次确认 import 不炸。
- [ ] `backend/Dockerfile` 监听 **8000**（dev 是 8002），下面 manifest 按 8000 写。
- [ ] ffmpeg 已在镜像里 ✅

---

## 3. Namespace / 配置 / 密钥

- [ ] 建 namespace
      ```bash
      kubectl create namespace video-maker
      ```

- [ ] **ConfigMap**（对齐 `deploy/config.yml`）
      ```bash
      kubectl -n video-maker create configmap video-maker-config \
        --from-literal=GEMINI_SCRIPT_MODEL=gemini-3.1-pro-preview \
        --from-literal=GEMINI_DIRECTOR_MODEL=gemini-3.1-pro-preview \
        --from-literal=VIDEO_PROVIDER=vertex \
        --from-literal=VEO_MODEL=veo-3.1-fast-generate-001 \
        --from-literal=WORKER_POOL_SIZE=4 \
        --from-literal=LANGFUSE_ENABLED=true \
        --from-literal=LANGFUSE_HOST=https://us.cloud.langfuse.com \
        --from-literal=DATABASE_URL='sqlite+aiosqlite:////data/app.db' \
        --from-literal=REDIS_URL=redis://redis:6379 \
        --from-literal=COS_REGION=ap-guangzhou \
        --from-literal=COS_BUCKET=<prod 桶，形如 video-maker-prod-1414782845> \
        --from-literal=COS_SCHEME=https \
        --from-literal=COS_AUTH_MODE=cvm_role \
        --from-literal=COS_CVM_ROLE=<绑在 CVM 上的 CAM 角色名> \
        --from-literal=COS_SIGNED_URL_TTL_SEC=7200 \
        --from-literal=CORS_ORIGINS='https://video.kuanzw.com'
      ```

      > **COS 桶**：`config.yml` 里那个是 dev 桶（`video-maker-dev-1414782845`），
      > 代码注释明确写了「dev / prod 使用不同 bucket」。**新建 prod 桶，别复用 dev 桶。**

      > **`COS_AUTH_MODE=cvm_role`**：节点本身就是腾讯云 CVM，给它绑一个 CAM 角色就能取
      > STS 临时密钥，**不用把永久密钥塞进集群**。
      > ⚠️ 需验证 pod 能否访问 CVM 元数据服务（`169.254.0.23`）—— flannel 下通常可达，
      > 但要实测。不通就退回 `COS_AUTH_MODE=static` + 下面的 secret。

      > ⚠️ **`CORS_ORIGINS` 必须精确匹配**。后端 `allow_credentials=True`，
      > 所以**不能用通配符 `*`**，且 `cors_origins.split(",")` 不做 trim ——
      > **逗号后面不能有空格**。
      > Vercel 的 preview 部署每次域名都变（`xxx-<hash>.vercel.app`），
      > preview 环境会 CORS 失败；demo 只用 production 域名即可。

- [ ] **Secret**
      ```bash
      kubectl -n video-maker create secret generic video-maker-secrets \
        --from-file=gemini_api_key=deploy/secrets/gemini_api_key \
        --from-file=veo_api_key=deploy/secrets/veo_api_key \
        --from-file=deepseek_api_key=deploy/secrets/deepseek_api_key \
        --from-file=langfuse_public_key=deploy/secrets/langfuse_public_key \
        --from-file=langfuse_secret_key=deploy/secrets/langfuse_secret_key
        # 若 cvm_role 走不通，再加：
        # --from-file=cos_secret_id=deploy/secrets/cos_secret_id \
        # --from-file=cos_secret_key=deploy/secrets/cos_secret_key

      kubectl -n video-maker create secret generic video-maker-gcp-sa \
        --from-file=gcp-sa.json=deploy/secrets/gcp-sa.json
      ```
      > ⚠️ **注意文件末尾换行**：`make secrets` 写出的文件若带 `\n`，注入后 key 会多一个换行导致 401。
      > 灌之前 `xxd deploy/secrets/gemini_api_key | tail -1` 看一眼。

---

## 4. 存储

**媒体在 COS，不占集群存储。**集群里只剩 sqlite 需要落盘。

- [ ] 一个 PVC 即可：
      ```yaml
      apiVersion: v1
      kind: PersistentVolumeClaim
      metadata: { name: app-data, namespace: video-maker }
      spec:
        accessModes: [ReadWriteOnce]
        resources: { requests: { storage: 2Gi } }
      ```
- [ ] worker 做 ffmpeg 合并需要临时空间 → 用 **`emptyDir`** 挂 `/tmp`，不要再开 PVC
- [ ] `local-path` 是 `WaitForFirstConsumer`，PVC 会绑在第一个消费它的 pod 所在节点；
      因为所有 pod 都钉在 `vm-0-8-ubuntu`，PVC 自动落该节点本地盘 ✅
- [ ] ⚠️ **sqlite 单写者**：backend 和 worker 写同一个库，**两者都必须 `replicas: 1`**
      且同节点（nodeSelector 已保证）。RWO PVC 同节点多 Pod 挂载是允许的。
- [ ] ⚠️ `local-path` reclaimPolicy=**Delete** —— 删 PVC 数据就没了，demo 可接受。

### COS 桶配置

- [ ] 新建 prod 桶，与 CVM **同地域**（`ap-guangzhou`）
- [ ] 给 CVM 绑 CAM 角色，授予该桶的读写权限
- [ ] **桶的跨域（CORS）规则**要允许 `https://video.kuanzw.com`
      （SPA 从 Vercel 域名去取 COS 上的签名 URL，是跨域请求）
- [ ] 桶保持**私有读**（依赖预签名 URL，TTL 2h）

---

## 5. 工作负载

三个 Deployment，全部加：

```yaml
      nodeSelector:
        kubernetes.io/hostname: vm-0-8-ubuntu
      imagePullSecrets:
        - name: dockerhub-pull-secret
```

- [ ] **redis** — `replicas: 1`，`redis:7-alpine` + Service `redis` ClusterIP :6379
      （demo 不做持久化，重启丢队列可接受）
- [ ] **backend** — `replicas: 1`，`docker.io/i6o6i/video-maker-backend:demo`
      - containerPort 8000（**不要 hostPort**，入口交给 traefik）
      - `envFrom`: configMapRef `video-maker-config` + secretRef `video-maker-secrets`
      - `env`: `GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/gcp-sa.json`
        + `HTTPS_PROXY=<节点代理>`、`NO_PROXY=redis,localhost,127.0.0.1,.svc,169.254.0.23,.myqcloud.com`
        > ⚠️ **COS 和元数据服务必须绕过代理**，否则内网直连变成走代理，既慢又可能失败
      - volumeMounts：`app-data → /data`、`emptyDir → /tmp`、
        secret `video-maker-gcp-sa → /run/secrets`（readOnly）
      - `readinessProbe: httpGet /health :8000`（**`/health` 在根路径，不在 `/api/` 下**）
      + Service `backend` ClusterIP :8000
- [ ] **worker** — 同一镜像，`replicas: 1`，
      `command: ["python","-m","arq","worker.arq_worker.WorkerSettings"]`，
      挂载 / env 与 backend 完全一致
- [ ] ~~frontend~~（去 Vercel）、~~vc-worker~~、~~mcp~~、~~playwright~~ —— 不部署

---

## 6. Ingress（把 traefik 挪到 vm-0-8-ubuntu）

k3s 的 traefik 由 HelmChartConfig 管理。**`valuesContent` 是整体替换，不是合并** ——
必须把现有值一起写回去，否则会丢配置。

现有值：
```yaml
nodeSelector:
  kubernetes.io/hostname: cool-cube-1.localdomain
ports:
  websecure:
    expose:
      default: false
service:
  annotations:
    svccontroller.k3s.cattle.io/lbpool: web
```

- [ ] 改成（**改了 nodeSelector，加了 hostPort，其余原样保留**）：
      ```yaml
      apiVersion: helm.cattle.io/v1
      kind: HelmChartConfig
      metadata:
        name: traefik
        namespace: kube-system
      spec:
        valuesContent: |-
          nodeSelector:
            kubernetes.io/hostname: vm-0-8-ubuntu
          ports:
            web:
              hostPort: 80
            websecure:
              expose:
                default: false
          service:
            annotations:
              svccontroller.k3s.cattle.io/lbpool: web
      ```
      > hostPort 80 → 容器内 `web` entrypoint 是 **8000**（非特权端口），
      > 所以不需要 `NET_BIND_SERVICE`，也不需要 hostNetwork。

- [ ] 应用后等 helm-controller 跑完升级，确认 traefik 落到目标节点
      ```bash
      kubectl -n kube-system rollout status deploy/traefik
      kubectl -n kube-system get pod -l app.kubernetes.io/name=traefik -o wide
      ```

- [ ] **Ingress 资源**（backend 自己就期望 `/api` 前缀，**不要加 rewrite**）
      ```yaml
      apiVersion: networking.k8s.io/v1
      kind: Ingress
      metadata:
        name: video-maker-api
        namespace: video-maker
      spec:
        ingressClassName: traefik
        rules:
          - host: api.video.kuanzw.com
            http:
              paths:
                - path: /api
                  pathType: Prefix
                  backend:
                    service: { name: backend, port: { number: 8000 } }
                - path: /health
                  pathType: Exact
                  backend:
                    service: { name: backend, port: { number: 8000 } }
      ```

### 注意

- traefik **默认不缓冲响应**，SSE 开箱即用 ✅（不像 ingress-nginx 要加 `proxy-buffering: off` 注解）
- traefik 默认 **无请求体大小限制** ✅
- ⚠️ traefik `respondingTimeouts.idleTimeout` 默认 **180s** —— SSE 心跳间隔必须小于它
- ⚠️ 挪走 traefik 后，另两个节点（racknerd / cool-cube）的 svclb 会把 80 的流量跨洲转发到
  国内节点。当前无人使用所以无影响，但**以后若要在美国节点上跑 ingress 应用，需要重新规划**。

---

## 7. Cloudflare

Vercel 强制 https，而源站是 http:80 —— 不套 TLS 的话浏览器会以**混合内容**拦掉所有请求。
用 Cloudflare Flexible 在边缘补上 https。

- [ ] DNS：`api.video.kuanzw.com` **A** → `139.199.78.140`，**开启橙云代理**
- [ ] SSL/TLS 模式设为 **Flexible**（浏览器↔CF 走 https，CF↔源站走 http:80）
- [ ] ⚠️ **加一条 Cache Rule：`api.video.kuanzw.com/*` → Bypass cache**。
      素材接口返回的是 **302 + 2 小时过期的签名 URL**，被 CF 缓存住会导致过期后集体 403。
- [ ] ⚠️ **免费版代理超时约 100s** —— SSE 心跳间隔必须小于 100s，否则进度流会被掐断
- [ ] ⚠️ **免费版请求体上限 100MB** —— 媒体走 COS 直读不受影响，但参考图/音频**上传**走 API，注意上限
- [ ] 需要真实客户端 IP 时读 `CF-Connecting-IP`
- [ ] ⚠️ **Flexible 模式下源站仍是明文**，公网可直接访问 `http://139.199.78.140/` 绕过 CF。
      demo 可接受，但要清楚 API 实际是裸奔的。

---

## 8. Vercel（前端静态托管）

- [ ] 导入仓库，**Root Directory 设为 `frontend-vite`**，框架预设 Vite
      （build `npm run build`，输出 `dist`）
- [ ] 环境变量（Production）：
      ```
      VITE_API_BASE = https://api.video.kuanzw.com
      ```
      > Vite 的 env 是**构建时**注入的，改了必须重新部署才生效。
- [ ] 绑定域名 `video.kuanzw.com`（CNAME → `cname.vercel-dns.com`）
- [ ] 若深链接（如 `/projects/xxx`）404，加 `frontend-vite/vercel.json` 做 SPA 回退：
      ```json
      { "rewrites": [{ "source": "/(.*)", "destination": "/index.html" }] }
      ```
- [ ] ⚠️ 前端**不需要**任何反代配置 —— `VITE_API_BASE` 一给，54 个接口自动变绝对地址，
      SSE（`sse.ts`）用的是同一个 BASE

---

## 9. 部署后验证

- [ ] Pod 全 Running 且都在目标节点
      ```bash
      kubectl -n video-maker get pods -o wide      # NODE 列全是 vm-0-8-ubuntu
      kubectl -n kube-system get pod -l app.kubernetes.io/name=traefik -o wide
      ```
- [ ] PVC Bound：`kubectl -n video-maker get pvc`
- [ ] 后端直连：`kubectl -n video-maker exec deploy/backend -- curl -s localhost:8000/health`
- [ ] 过 traefik（集群内，带 Host 头）：
      ```bash
      kubectl -n video-maker run t --rm -it --image=curlimages/curl --restart=Never -- \
        curl -s -H 'Host: api.video.kuanzw.com' http://traefik.kube-system/api/projects
      ```
- [ ] 节点本机：`ssh <node> "curl -s -H 'Host: api.video.kuanzw.com' localhost:80/api/projects"`
- [ ] 绕过 CF 直连源站：`curl -s -H 'Host: api.video.kuanzw.com' http://139.199.78.140/api/projects`
- [ ] 过 Cloudflare：
      ```bash
      curl -sI https://api.video.kuanzw.com/api/projects   # 期望 200，且 cf-cache-status: BYPASS/DYNAMIC
      ```
- [ ] 前端：浏览器打开 `https://video.kuanzw.com`
      - 项目列表能加载 → 跨域 CORS 通了
      - **F12 Console 无 mixed content 报错** → CF https 生效
      - 打开一个已有项目，视频能播 → COS 签名 URL + 桶 CORS 通了
      - 进度流不断线 → SSE 过了 CF 100s 与 traefik 180s 两道超时

---

## 10. 故障对照表

| 现象 | 大概率原因 |
|---|---|
| pod 全 Pending | 节点还没 uncordon |
| traefik Pending | hostPort 80 被占（宿主机 nginx，或误打了 `enablelb` 标签） |
| 直连源站不通 | 腾讯云安全组没放行 80 |
| 浏览器 console 报 mixed content | CF 没开橙云 / SSL 模式不是 Flexible |
| 接口 CORS 报错 | `CORS_ORIGINS` 没填 `https://video.kuanzw.com`，或逗号后带了空格 |
| 前端请求打到 Vercel 自己域名 | `VITE_API_BASE` 没设，或设了但没重新构建（Vite 是构建时注入） |
| 视频一开始能播，2 小时后集体 403 | CF 把 302 签名 URL 缓存住了 → 加 Bypass cache 规则 |
| 视频压根不能播 | COS 桶 CORS 没允许 `https://video.kuanzw.com`，或桶/角色权限不对 |
| 进度流跑一会儿断 | SSE 心跳 > CF 100s 或 > traefik idleTimeout 180s |
| 生成任务一直排队不动 | worker 没起 / `REDIS_URL` 不对 |
| 生成报 Google API 超时 | 国内节点没配代理（见 §1） |
| COS 上传/读取超时 | `NO_PROXY` 没排除 `.myqcloud.com`，内网请求被塞进代理了 |
| API key 401 | secret 文件末尾多了换行 |
| ImagePullBackOff | ghcr.io 国内拉不动 → 换 Docker Hub + `dockerhub-pull-secret` |
| backend CrashLoop，ImportError | `backend/Dockerfile` 手写 pip 列表缺 `langfuse` / COS SDK（见 §2） |

---

## 11. demo 明确不做的事

- ❌ 源站 TLS（靠 CF 边缘；要端到端加密再上 cert-manager 或 CF Full 模式）
- ❌ 多副本 / HA（sqlite 单写者 + RWO PVC，天然单副本）
- ❌ 数据备份（`local-path` reclaimPolicy=Delete，删 PVC 即丢；媒体在 COS 相对安全）
- ❌ 资源 limits 调优（先跑起来，OOM 了再加）
- ❌ vc-worker（变声）、mcp server、playwright
- ❌ 认证 —— **API 对公网开放，任何人都能消耗你的 Veo/Gemini 额度**。
      demo 期间建议至少用 Cloudflare Access 或 WAF 规则限制来源，
      用完 `kubectl -n video-maker scale deploy --all --replicas=0`。
