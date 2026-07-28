# Video Maker 上云清单（k3s / 单节点 demo）

**目标**：把 video_maker 部署到 k3s 集群，**只调度到 `vm-0-8-ubuntu`**，
**直接用 hostPort 暴露 80 到公网**，域名 `video.kuanzw.com` 直接 A 记录指向该节点公网 IP。

**定位**：普通 demo。单副本、无 HA、无 TLS、DB 直接跑 pod、存储用节点本地盘。
不做 Ingress / cert-manager / 多副本 / 备份。

---

## 0. 集群现状（已实地核对，2026-07-28）

| 项 | 值 |
|---|---|
| 集群 | k3s v1.35.4+k3s1，3 节点，context `racknerd` |
| 目标节点 | `vm-0-8-ubuntu` |
| 内网 IP | `10.200.0.2` |
| 公网 IP | **`139.199.78.140`**（腾讯云 CVM，国内） |
| 节点状态 | ⚠️ **`Ready,SchedulingDisabled`**（已 cordon，`unschedulable: true`） |
| 节点标签 | `kubernetes.io/hostname=vm-0-8-ubuntu`、`role=monitoring` |
| 当前 Pod 数 | **0**（节点上什么都没跑） |
| 默认 StorageClass | `local-path`（rancher.io/local-path，`WaitForFirstConsumer`） |
| Ingress | kube-system 有 traefik，但 **不在这个节点上** |

### 关键结论：80 端口在 `vm-0-8-ubuntu` 上是空的 ✅

k3s 的 `svclb-traefik` DaemonSet 抢的就是 hostPort 80，但它带
`nodeSelector: svccontroller.k3s.cattle.io/enablelb=true`，而 `vm-0-8-ubuntu`
**没有这个标签** —— 所以即使 uncordon 之后，svclb 也不会调度过来抢 80。

> ⚠️ 反过来说：**永远不要给 `vm-0-8-ubuntu` 打 `svccontroller.k3s.cattle.io/enablelb=true` 标签**，
> 一打上 svclb 就会来抢 hostPort 80，我们的 frontend pod 会 Pending。

---

## 1. 上线前置条件（这几项不做，后面全白搭）

- [ ] **解除节点 cordon**（当前带 `node.kubernetes.io/unschedulable:NoSchedule` 污点，
      不解除的话所有 pod 都 Pending）
      ```bash
      kubectl uncordon vm-0-8-ubuntu
      kubectl get node vm-0-8-ubuntu   # 期望 Ready，不再有 SchedulingDisabled
      ```
      > 备选（不想全局放开调度时）：保持 cordon，给所有 Deployment 加
      > `tolerations: [{key: node.kubernetes.io/unschedulable, operator: Exists, effect: NoSchedule}]`。
      > demo 建议直接 uncordon，简单。

- [ ] **腾讯云安全组放行 TCP 80**（入方向，源 `0.0.0.0/0`）。
      控制台 → 该 CVM → 安全组 → 入站规则。**这一步最容易漏，漏了表现为「pod 全 Running 但外网连不上」。**

- [ ] **确认节点本机 80 没被占用**（宿主机上可能跑着 nginx/caddy）
      ```bash
      ssh <node> "sudo ss -lntp | grep ':80 '"   # 期望无输出
      ```

- [ ] **DNS**：`video.kuanzw.com` A 记录 → `139.199.78.140`
      ```bash
      dig +short video.kuanzw.com   # 期望 139.199.78.140
      ```

- [ ] **确认国内节点能访问 Google API**。后端要调 Vertex AI（Veo / Gemini），
      节点在腾讯云国内 —— **直连必然失败**。二选一：
      - 节点上跑代理，容器里注入 `HTTPS_PROXY`（dev 环境就是这么干的：`host.containers.internal:10809`）；
      - 或 demo 阶段先不演示生成，只演示已有素材的浏览 / 剪辑。

      > 这是**唯一可能让 demo「跑起来但生成不了」的硬伤**，提前定方案。

---

## 2. 镜像

现有 `Makefile` 已经有 build/push（`REGISTRY=ghcr.io`、`NAMESPACE=$USER`）。
但节点在国内，**ghcr.io 拉取大概率超时**。

- [ ] 改用 Docker Hub（集群里已有成功先例：`tarot` ns 用 `docker.io/i6o6i/ai-tarot-backend:latest`
      + `imagePullSecrets: dockerhub-pull-secret`）
      ```bash
      make build-frontend build-backend REGISTRY=docker.io NAMESPACE=i6o6i TAG=demo
      make push          REGISTRY=docker.io NAMESPACE=i6o6i TAG=demo
      ```
- [ ] 把拉取 secret 复制到新 namespace
      ```bash
      kubectl get secret dockerhub-pull-secret -n tarot -o yaml \
        | sed 's/namespace: tarot/namespace: video-maker/' \
        | kubectl apply -f -
      ```

### ⚠️ 镜像本身的两个坑（上线前必须确认）

- [ ] **`backend/Dockerfile` 的依赖是手写死的 pip 列表**，不是 `pyproject.toml`：
      ```
      fastapi uvicorn sqlalchemy aiosqlite arq redis pydantic pydantic-settings
      google-genai ffmpeg-python python-multipart sse-starlette python-json-logger
      ```
      而 `config.yml` 里 `LANGFUSE_ENABLED=true`，代码还用到 `langfuse`、`fastmcp` 等。
      **先在本地 `podman run` 起一次镜像验证 import 不炸**，或者干脆改成
      `uv sync --project .`（推荐，和 dev 保持一致）。
- [ ] `backend/Dockerfile` 监听 **8000**（dev 是 8002）。下面 manifest 按 8000 写，别混。
- [ ] `vc-worker` 镜像烤了 ~2.6GB ONNX 模型 —— demo 如果不演示变声，**这个服务直接不部署**，
      省事省盘。

---

## 3. Namespace / 配置 / 密钥

- [ ] 建 namespace
      ```bash
      kubectl create namespace video-maker
      ```
- [ ] **ConfigMap**（来自 `deploy/config.yml`，非敏感）
      ```bash
      kubectl -n video-maker create configmap video-maker-config \
        --from-literal=GEMINI_SCRIPT_MODEL=gemini-3.1-pro-preview \
        --from-literal=GEMINI_DIRECTOR_MODEL=gemini-3.1-pro-preview \
        --from-literal=VIDEO_PROVIDER=vertex \
        --from-literal=VEO_MODEL=veo-3.1-fast-generate-001 \
        --from-literal=WORKER_POOL_SIZE=4 \
        --from-literal=LANGFUSE_ENABLED=true \
        --from-literal=LANGFUSE_HOST=https://us.cloud.langfuse.com \
        --from-literal=STORAGE_ROOT=/workspace/storage \
        --from-literal=DATABASE_URL='sqlite+aiosqlite:////data/app.db' \
        --from-literal=REDIS_URL=redis://redis:6379 \
        --from-literal=CORS_ORIGINS='http://video.kuanzw.com,http://139.199.78.140'
      ```
      > ⚠️ **`CORS_ORIGINS` 必须加上 `http://video.kuanzw.com`**，
      > 现在只有 localhost/127.0.0.1，不加前端跨域全挂。

- [ ] **Secret**（直接从 `deploy/secrets/` 目录灌，key 名和文件名一致）
      ```bash
      kubectl -n video-maker create secret generic video-maker-secrets \
        --from-file=gemini_api_key=deploy/secrets/gemini_api_key \
        --from-file=veo_api_key=deploy/secrets/veo_api_key \
        --from-file=kie_api_key=deploy/secrets/kie_api_key \
        --from-file=deepseek_api_key=deploy/secrets/deepseek_api_key \
        --from-file=langfuse_public_key=deploy/secrets/langfuse_public_key \
        --from-file=langfuse_secret_key=deploy/secrets/langfuse_secret_key

      kubectl -n video-maker create secret generic video-maker-gcp-sa \
        --from-file=gcp-sa.json=deploy/secrets/gcp-sa.json
      ```
      > 注意文件末尾换行：`make secrets` 写出来的文件如果带 `\n`，
      > 用 `envFrom`/`env` 注入后 key 会多个换行导致鉴权 401。灌之前
      > `xxd deploy/secrets/gemini_api_key | tail -1` 看一眼。

---

## 4. 存储（DB 直接跑 pod，盘用节点本地）

`local-path` 是 `WaitForFirstConsumer`，PVC 会绑在第一个消费它的 pod 所在节点上 ——
因为所有 pod 都 nodeSelector 钉在 `vm-0-8-ubuntu`，**PVC 自动落在该节点本地盘**，符合预期。

RWO 单节点多 Pod 挂载：backend / worker 都在同一节点，**可以共享同一个 RWO PVC**。

- [ ] 两个 PVC：

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: app-data, namespace: video-maker }     # sqlite
spec:
  accessModes: [ReadWriteOnce]
  resources: { requests: { storage: 2Gi } }
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: app-storage, namespace: video-maker }  # 视频/音频/帧图，会长
spec:
  accessModes: [ReadWriteOnce]
  resources: { requests: { storage: 50Gi } }
```

- [ ] **先确认节点磁盘够**：`ssh <node> df -h /var/lib/rancher/k3s/storage`
      （本地 dev 的 storage 已经 385MB，一个项目几十个 shot 很快上 GB）
- [ ] `local-path` 的 reclaimPolicy 是 **Delete** —— 删 PVC 数据就没了，demo 可接受，心里有数。

### DB 说明

- **sqlite**：不是独立 pod，是 backend/worker 挂 `app-data` PVC 里的文件。
  ⚠️ **sqlite 单写者**：backend 和 worker 都写同一个库，**两者都必须 `replicas: 1`**，
  且必须在同一节点（已由 nodeSelector 保证）。
- **redis**：独立 pod（见下），demo 不做持久化，重启丢队列可接受。

---

## 5. 工作负载（全部 nodeSelector 钉到 `vm-0-8-ubuntu`）

所有 Deployment 统一加：

```yaml
      nodeSelector:
        kubernetes.io/hostname: vm-0-8-ubuntu
      imagePullSecrets:
        - name: dockerhub-pull-secret
```

- [ ] **redis** — Deployment `replicas: 1`，image `redis:7-alpine`
      + Service `redis` ClusterIP :6379
- [ ] **backend** — `replicas: 1`，image `docker.io/i6o6i/video-maker-backend:demo`
      - port 8000
      - `envFrom`: configMapRef `video-maker-config` + secretRef `video-maker-secrets`
      - `env`: `GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/gcp-sa.json`
        （+ 需要代理时的 `HTTPS_PROXY` / `NO_PROXY=redis,localhost,127.0.0.1,.svc`）
      - volumeMounts：`app-data → /data`、`app-storage → /workspace/storage`、
        secret `video-maker-gcp-sa → /run/secrets`（readOnly）
      - `readinessProbe: httpGet /health :8000`
      + Service `backend` ClusterIP :8000
- [ ] **worker** — 同一个 backend 镜像，`replicas: 1`，
      `command: ["python","-m","arq","worker.arq_worker.WorkerSettings"]`，
      挂载 / env 与 backend 完全一致（同样要 gcp-sa + 代理）
- [ ] **frontend** — `replicas: 1`，image `docker.io/i6o6i/video-maker-frontend:demo`
      —— **这是公网入口**，见下一节
- [ ] ~~vc-worker~~ / ~~mcp~~ / ~~playwright~~ —— demo 不部署

---

## 6. 暴露 80 到公网（hostPort，不走 Ingress）

frontend 是 nginx 静态站 + `/api/` 反代，直接让它占节点的 80：

```yaml
      containers:
        - name: frontend
          image: docker.io/i6o6i/video-maker-frontend:demo
          ports:
            - containerPort: 80
              hostPort: 80        # ← 直接绑节点 80，外网 139.199.78.140:80 即可访问
```

- [ ] 不建 Ingress、不建 LoadBalancer Service（会触发 svclb 抢端口）
- [ ] frontend 仍建一个 ClusterIP Service（方便集群内 debug，可选）
- [ ] `replicas` 必须是 **1**（hostPort 同节点唯一，2 副本第二个必 Pending）

### ⚠️ frontend nginx.conf 必须先改（现在是坏的）

`frontend-vite/nginx.conf` 现状有两个问题：

1. 后端端口写的 **8000**（和生产镜像一致 ✅），但 dev 是 8002 —— 确认用 8000。
2. **路径前缀不一致**：
   ```nginx
   location /api/          { proxy_pass http://backend:8000/;          }  # ← 吃掉了 /api 前缀
   location /api/projects/ { proxy_pass http://backend:8000/api/projects/; }  # ← 保留前缀
   ```
   后端路由全在 `/api/` 下，第一条会把 `/api/xxx` 转成 `/xxx` → **404**。

- [ ] 改成统一保留前缀（对齐 `deploy/nginx.conf` 的写法）：
      ```nginx
      location /api/ {
          proxy_pass http://backend:8000/api/;
          proxy_http_version 1.1;
          proxy_set_header Host $host;
          proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
          proxy_buffering off;      # SSE 进度流必需
          proxy_cache off;
          proxy_read_timeout 300s;
      }
      ```
- [ ] **SSE 必须关 buffering** —— 不关的话生成进度条前端一直不动（这个最坑，表现像"卡住"）
- [ ] 媒体文件（`/storage/...`）的访问路径确认走后端还是 nginx 直出，别漏配

---

## 7. 部署后验证

- [ ] 全部 Running 且都在目标节点
      ```bash
      kubectl -n video-maker get pods -o wide
      # NODE 列必须全是 vm-0-8-ubuntu
      ```
- [ ] PVC 已 Bound
      ```bash
      kubectl -n video-maker get pvc
      ```
- [ ] 集群内自测（注意：`/health` 在**根路径**，不在 `/api/` 下；
      经 nginx 走的必须用真实 API 路径 `/api/projects`）
      ```bash
      kubectl -n video-maker exec deploy/backend  -- curl -s localhost:8000/health      # 后端直连
      kubectl -n video-maker exec deploy/frontend -- wget -qO- localhost/api/projects   # 过 nginx 反代
      ```
- [ ] 节点本机
      ```bash
      ssh <node> "curl -sI localhost:80"
      ```
- [ ] 公网 IP
      ```bash
      curl -sI http://139.199.78.140/
      curl -s  http://139.199.78.140/api/projects
      ```
- [ ] 域名
      ```bash
      curl -sI http://video.kuanzw.com/
      ```
- [ ] 浏览器打开 `http://video.kuanzw.com` —— 项目列表能加载（说明 `/api/` 反代 + CORS 都对）
- [ ] 打开一个已有项目，SSE 进度流不断线（说明 buffering 关了）

---

## 8. 常见故障对照表

| 现象 | 大概率原因 |
|---|---|
| pod 全 Pending | 节点还没 uncordon |
| frontend Pending，其它 Running | hostPort 80 被占（宿主机 nginx，或误打了 `enablelb` 标签） |
| pod 全 Running，外网连不上 | 腾讯云安全组没放行 80 |
| 页面出来了，接口 404 | `nginx.conf` 的 `/api/` 把前缀吃掉了（见 §6） |
| 页面出来了，接口 CORS 报错 | `CORS_ORIGINS` 没加 `http://video.kuanzw.com` |
| 生成任务一直排队不动 | worker 没起 / `REDIS_URL` 不对 |
| 生成任务报 Google API 超时 | 国内节点没代理（见 §1 最后一项） |
| API key 401 | secret 文件末尾多了换行 |
| ImagePullBackOff | ghcr.io 国内拉不动 → 换 Docker Hub + `dockerhub-pull-secret` |
| backend CrashLoop，ImportError | `backend/Dockerfile` 手写 pip 列表缺包（见 §2） |

---

## 9. demo 明确不做的事

- ❌ HTTPS / TLS（纯 http 80；要 TLS 再上 cert-manager 或直接 Cloudflare 代理）
- ❌ 多副本 / HA（sqlite 单写者 + RWO PVC + hostPort，天然单副本）
- ❌ 数据备份（local-path reclaimPolicy=Delete，删 PVC 即丢）
- ❌ 资源 limits 调优（先跑起来，OOM 了再加）
- ❌ vc-worker（变声）、mcp server、playwright
- ❌ 认证 —— **80 直接对公网，任何人都能用你的 Veo/Gemini 额度**。
      demo 期间建议至少在 nginx 加个 basic auth，或用完就 `kubectl scale --replicas=0`。
