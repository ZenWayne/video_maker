# k3s deployment — video_maker

Manifests for deploying `video_maker` (backend + arq worker + Vite frontend +
in-cluster Redis) to the existing k3s cluster, pinned entirely to node
**`vm-0-8-ubuntu`** (10.200.0.2 internal / 139.199.78.140 external, labeled
`role=monitoring`).

## Layout

| File | Purpose |
|---|---|
| `00-namespace.yaml` | Creates the `video-maker` namespace |
| `10-configmap.yaml` | Non-secret env — mirrors `deploy/config.yml`, plus cluster-internal wiring (`REDIS_URL`, `DATABASE_URL`, `GOOGLE_APPLICATION_CREDENTIALS`) |
| `20-secret.example.yaml` | Template for `video-maker-secrets` (API keys). Fill in real values → `20-secret.yaml`. Do not commit. |
| `video-maker-gcp-sa` (no file) | GCP SA key for Vertex AI (Gemini/Veo/CC/TF). Created imperatively, never committed. |
| `25-pvc.yaml` | `video-maker-data` (1Gi, sqlite DB, mounted in backend+worker) and `video-maker-whisper-cache` (5Gi, faster-whisper weights, worker only) |
| `30-redis.yaml` | Redis `Deployment` + `Service` (arq broker) |
| `40-backend.yaml` | FastAPI backend `Deployment` + `Service` (named `backend` — see note below) |
| `50-worker.yaml` | arq worker `Deployment` (ASR + generation jobs, no Service — no HTTP port) |
| `60-frontend.yaml` | nginx/Vite frontend `Deployment` + `Service` |
| `80-ingressroute.yaml` | Traefik `IngressRoute`, HTTP only, placeholder host |

Skipped on purpose: `vc-worker` (voice-conversion model, not needed for this
deployment) and `playwright`/`mcp` (dev-only tooling).

## Why everything is pinned to `vm-0-8-ubuntu`

`nodeSelector: kubernetes.io/hostname: vm-0-8-ubuntu` is set on every
workload here because that's the node this deployment was scoped to. The
`tarot` project's backend (`ai_tarot_backend/deploy/k8s/30-deployment.yaml`)
intentionally uses a `nodeAffinity` that **excludes** this same node
(`role NotIn [monitoring]`) because it can't reach `docker.io` from there.
**Do not** add a taint, cordon, or otherwise change scheduling on
`vm-0-8-ubuntu` as part of this deployment — that would break `tarot`, which
deliberately avoids it, and disrupt the monitoring stack (grafana/prometheus/
tempo/alloy-gateway) that already runs there.

## Resource budget (see also "Memory risk" below)

Node capacity: 4 CPU / 3.8Gi RAM (`3808592Ki` allocatable). Already committed
by the monitoring stack: ~640Mi requests / 1536Mi limits. That leaves ample
headroom by *requests* (~3.1Gi), but the task brief for this deployment
budgets conservatively at **~2.2Gi realistically free** — treat that as the
working number since real usage (not just requests) is what actually matters
under memory pressure.

| Component | Requests | Limits | Why |
|---|---|---|---|
| redis | 10m / 32Mi | 250m / 128Mi | trivial in-memory queue |
| backend | 100m / 128Mi | 500m / 512Mi | FastAPI, I/O-bound (Vertex AI / COS calls) |
| worker | 500m / **1Gi** | 2000m / **2560Mi** | faster-whisper `large-v3` int8 resident ~1.5–2Gi + arq pool (`WORKER_POOL_SIZE=4`) |
| frontend | 10m / 16Mi | 200m / 64Mi | static nginx |
| **Total** | **620m / ~1.17Gi** | **2950m / ~3.19Gi** | |

Total *requests* (~1.17Gi) fit comfortably under the ~2.2Gi free budget.
Total *limits* (~3.19Gi) do **not** — that's expected and matches how the
monitoring stack itself is already provisioned (limits 41% of node capacity,
i.e. already over a strict per-workload budget); limits are burst headroom,
not a reservation. The worker is the pod actually at risk of being OOMKilled
under real memory pressure — see below.

## Image prerequisites (previously "known gaps" — now fixed)

The manifests were originally written without touching any Dockerfile, which
surfaced several blockers. All of them are fixed in this branch; recorded here
because the fixes are what make the images runnable outside docker-compose.

1. **`deploy/Dockerfile.worker` was dev-only** — it installed `ffmpeg` on top
   of the `uv` base image and nothing else: no source, no dependency install.
   That works in dev only because `docker-compose.dev.yml` bind-mounts
   `../backend` at `/app` and `uv run --project . --group asr` lazily resolves
   against the mounted `pyproject.toml`/`uv.lock`. There is no bind mount in
   k8s, so a standalone build crash-looped on a missing `/app/pyproject.toml`.
   It now `COPY`s the backend source and runs
   `uv sync --frozen --no-dev --group asr` at build time. Build context is
   `backend/`, not `deploy/`.

2. **`backend/Dockerfile` used `WORKDIR /workspace`** and a hand-maintained
   `pip install` list that had drifted from `backend/pyproject.toml` (missing
   `openai`, `httpx`, `scikit-image`, `langfuse`, `fastmcp`,
   `cos-python-sdk-v5` — all imported by current app code). It now uses
   `WORKDIR /app` and installs from `pyproject.toml` + `uv.lock`, so
   `DATABASE_URL=sqlite+aiosqlite:////app/data/dev.db` and the PVC mounted at
   `/app/data` line up with the worker, which mounts the same PVC at the same
   path. The *port* choice (8000, not the dev-compose 8002) is deliberate —
   see the comment at the top of `40-backend.yaml`: it matches the
   Dockerfile's own `CMD` and `frontend-vite/nginx.conf`'s `proxy_pass
   http://backend:8000/api/`, and the backend `Service` is named exactly
   `backend` so that upstream resolves.

3. **Both Python images run their venv directly** (`/app/.venv/bin` first on
   `PATH`) rather than via `uv run`, so containers never need network access
   at start just to resolve a lock.

4. **`frontend-vite/nginx.conf` had two problems**, both fixed:
   - The generic `location /api/` used `proxy_pass http://backend:8000/`,
     which *strips* the `/api` prefix — but every router in
     `backend/app/main.py` is mounted with `prefix="/api"`, so everything
     except the specially-cased `/api/projects/` would have 404'd. It now
     proxies to `http://backend:8000/api/`.
   - Only `/api/projects/` disabled SSE buffering. The content-analysis
     stream (`GET /api/analyses/{id}/stream`, used by
     `frontend-vite/src/lib/analysisSse.ts`) fell into the generic location
     and got buffered, so progress never arrived in real time. It now has its
     own location block with `proxy_buffering off`.

5. **`frontend-vite/Dockerfile` builds on `node:24-alpine`, not
   `node:20-alpine`.** `package-lock.json` is a lockfileVersion-3 lock written
   by npm 11; npm 10.8.x (shipped in node:20) computes a different ideal tree
   for the nested `vite@8` that `vitest@4` pulls in under a project pinned to
   `vite@5`, and `npm ci` fails with `Missing: esbuild@0.28.1 from lock file`.
   Matching the npm major that generated the lock keeps `npm ci` reproducible
   without rewriting the dependency tree.

6. **`.dockerignore` files were added** to `backend/` and `frontend-vite/`.
   The frontend one matters most: without it `COPY . .` drops the host's
   glibc-built `node_modules` (269 MB, with native `esbuild`/`rollup`
   binaries) on top of the musl `npm ci` install and breaks `npm run build`.

## Prerequisites

1. **Build + push the 3 images** (after fixing the Dockerfile gaps above).
   Log in to TCR first — credentials are yours to enter, not scripted:
   ```bash
   podman login ccr.ccs.tencentyun.com
   ```
   Then, from the repo root:
   ```bash
   # Backend (context: backend/)
   podman build -t ccr.ccs.tencentyun.com/tarrot/video-maker-backend:latest -f backend/Dockerfile backend/
   podman push ccr.ccs.tencentyun.com/tarrot/video-maker-backend:latest

   # Worker (context: backend/ — Dockerfile.worker needs a COPY of backend/ per "Known gaps" #1)
   podman build -t ccr.ccs.tencentyun.com/tarrot/video-maker-worker:latest -f deploy/Dockerfile.worker backend/
   podman push ccr.ccs.tencentyun.com/tarrot/video-maker-worker:latest

   # Frontend (context: frontend-vite/)
   podman build -t ccr.ccs.tencentyun.com/tarrot/video-maker-frontend:latest -f frontend-vite/Dockerfile frontend-vite/
   podman push ccr.ccs.tencentyun.com/tarrot/video-maker-frontend:latest
   ```

2. **TCR pull secret** — the cluster needs credentials to pull these private
   images:
   ```bash
   export KUBECONFIG=/home/wayne/kubeconfig
   kubectl create namespace video-maker --dry-run=client -o yaml | kubectl apply -f -
   kubectl -n video-maker create secret docker-registry tcr-pull-secret \
     --docker-server=ccr.ccs.tencentyun.com \
     --docker-username=<TCR_USERNAME> \
     --docker-password=<TCR_PASSWORD>
   ```

3. **GCP service-account JSON** for Vertex AI (Gemini / Veo / CC / TF —
   `google.genai.Client(vertexai=True, ...)` per project rules). Created
   imperatively, never committed:
   ```bash
   kubectl -n video-maker create secret generic video-maker-gcp-sa \
     --from-file=sa.json=<path-to-real-gcp-sa.json> \
     --dry-run=client -o yaml | kubectl apply -f -
   ```

## Deploy (apply order)

```bash
export KUBECONFIG=/home/wayne/kubeconfig

kubectl apply -f 00-namespace.yaml
kubectl apply -f 10-configmap.yaml

# Secrets — copy template, fill in from deploy/secrets.yml, apply, then delete
cp 20-secret.example.yaml 20-secret.yaml
$EDITOR 20-secret.yaml
kubectl apply -f 20-secret.yaml
shred -u 20-secret.yaml   # or just delete

# (video-maker-gcp-sa and tcr-pull-secret — see "Prerequisites" above)

kubectl apply -f 25-pvc.yaml
kubectl apply -f 30-redis.yaml
kubectl apply -f 40-backend.yaml
kubectl apply -f 50-worker.yaml
kubectl apply -f 60-frontend.yaml
kubectl apply -f 80-ingressroute.yaml
```

## Verify

```bash
kubectl -n video-maker get pods -o wide -w
kubectl -n video-maker logs deploy/video-maker-backend -f
kubectl -n video-maker logs deploy/video-maker-worker -f

# Backend health from inside the cluster
kubectl -n video-maker run curl-test --rm -it --image=curlimages/curl --restart=Never -- \
  curl -sS http://backend.video-maker.svc:8000/health

# Frontend through Traefik (resolve the placeholder host to one of the LB IPs)
curl -sS --resolve video-maker.example.com:80:107.174.159.18 http://video-maker.example.com/
```

## RESOLVED: pods on `vm-0-8-ubuntu` had no internet egress

**Status: fixed and verified end to end.** The `ip rule` below has been applied
to the node and the full content-analysis flow now passes (upload -> COS ->
transcribe -> Vertex brief, 171.9s). Keep reading for what was wrong and what
is still fragile.

```bash
# applied on vm-0-8-ubuntu — pod-sourced traffic now uses the default table
ip rule add from 10.42.0.0/16 lookup 1002 priority 10500
```

**That rule is in-memory only** — a node reboot, or `tailscaled` re-asserting
its rules, removes it and the app breaks again with `ENETUNREACH` on every COS
call. `deploy/k8s/optional/pod-egress-ip-rule-daemonset.yaml` watches for that
and re-adds it; it **has been applied** and is running on `vm-0-8-ubuntu`.

Verified by deleting the rule for real and watching it come back:

```
=== deleting the rule (simulating reboot / tailscaled reset) ===
  rule is GONE (expected)
=== egress should now be broken ===
  COS FAIL: OSError [Errno 101] Network is unreachable
=== waiting for the DaemonSet ===
  restored after ~10s
=== egress recovered? ===
  COS reachable again (0.74s)
```

> **Do not add `apk add iproute2` to that DaemonSet.** The first version did,
> and deadlocked: this pod's entire job is to repair egress, so on a node with
> broken egress the install hung forever and the repair loop never ran — the
> DaemonSet reported `Running`/`1 Ready` while doing nothing, and a manual
> delete of the rule was never repaired. BusyBox's built-in `ip` already
> supports `rule show/add/del`, so the pod needs no packages and works with no
> network at all. This was only caught by deleting the rule and watching;
> "the pod is Running" proved nothing.

It is a standing privileged / hostNetwork / NET_ADMIN pod on a shared node — it
only ever touches this one rule, but a systemd unit on the node is the
alternative if that trade-off is unwanted.

Final split, both halves verified:

| Traffic | Path | Measured |
|---|---|---|
| COS (media) | **direct**, `.myqcloud.com` in `NO_PROXY` | 1.8MB upload in 3.10s; job's own COS fetch 1s |
| Vertex AI + HuggingFace | via `egress-proxy` on `racknerd-b9d6fff` | `generate_content` 6.6s; HF 321 KiB/s |

HuggingFace is **blocked direct** from this node (connection reset), so model
weights must keep going through the proxy.

## Background: why it was broken

Found while running the real end-to-end check after deploying. All four pods
run fine, but `POST /api/analyses` fails at the COS upload:

```
Errno 101 Network is unreachable
-> video-maker-dev-1414782845.cos.ap-guangzhou.myqcloud.com:443
```

DNS resolves; every outbound TCP connect fails — including `www.qq.com`, so
this is not a GFW/Google issue.

### Measured

| From | www.qq.com | COS ap-guangzhou | Google (aiplatform) |
|---|---|---|---|
| pod netns on `vm-0-8-ubuntu` | ENETUNREACH | ENETUNREACH | ENETUNREACH |
| node netns (`hostNetwork`) on `vm-0-8-ubuntu` | 501 | 403 | **000 (unreachable)** |
| pod on `racknerd-b9d6fff` | 501 | 403 | 404 (reachable) |

### Root cause

The node uses Tailscale-style policy routing. `ip rule`:

```
11000: from all iif lo lookup 1002
17000: from all iif lo lookup 1002
31000: from all fwmark 0/0xffff iif lo lookup 1002
32000: from all lookup unspec unreachable      <- catch-all
```

Table `1002` holds the only real `default via 10.1.0.1 dev eth0`, but **every
rule that reaches it is qualified `iif lo`** — locally-originated traffic only.
Pod packets arrive `iif cni0`, match none of those rules, fall through to rule
32000 `unreachable`, and the node answers ICMP net-unreachable, which the pod's
socket surfaces as `ENETUNREACH`.

Flannel's masquerade is **not** the problem — `FLANNEL-POSTRTG` has the correct
`-s 10.42.0.0/16 ! -d 224.0.0.0/4 -j MASQUERADE`; packets simply never get to
the routing stage.

### Two independent problems

1. **Pod egress** (fixable on the node). A rule letting pod-sourced traffic use
   the default table, e.g. `ip rule add from 10.42.0.0/16 lookup 1002 priority
   10500`. Node-level change on a shared node, and tailscaled may re-assert its
   rules on restart, so it needs to be made persistent.
2. **Google is unreachable from this node at all** (`000` even from the node
   netns). Brief generation is hardcoded to Vertex AI —
   `worker/tasks.py` constructs `GeminiProvider(...)` directly, with no
   DeepSeek/OpenAI-compatible fallback — so **`出简报` cannot work on this node**
   even after fixing (1).

### The lever that avoids touching node routing

`ip rule 100: from all to 10.200.0.0/24 lookup main` is **not** `iif lo`
qualified, so pods *can* already reach the WireGuard tailnet (verified: pod ->
`10.200.0.1:80` OPEN, pod -> `10.200.0.2:22` OPEN). Pointing `HTTPS_PROXY` /
`HTTP_PROXY` at a proxy on `10.200.0.0/24` in `10-configmap.yaml` would give
both COS and Vertex egress with no node change — mirroring how
`docker-compose.dev.yml` already does it
(`HTTPS_PROXY: http://host.containers.internal:10809`,
`NO_PROXY: ...,.myqcloud.com`). No such proxy was listening at the time of
writing (10809/7890 refused on 10.200.0.1).

Remember to keep `NO_PROXY` covering in-cluster names
(`backend`, `redis`, `.svc`, `.cluster.local`, `10.42.0.0/16`, `10.43.0.0/16`)
so cluster traffic does not get sent to the proxy.

### What was actually deployed, and why it is not the final answer

`35-egress-proxy.yaml` deploys tinyproxy on `racknerd-b9d6fff` (the only
measured node with full internet **including Google**) and `10-configmap.yaml`
points `HTTP(S)_PROXY` at it. That makes both COS and Vertex reachable from
the app pods, verified end to end. It is the one deliberate exception to
"everything on `vm-0-8-ubuntu`" — unavoidable, because that node cannot reach
Google in any netns.

**But routing COS through it is impractically slow.** Every egress-capable
node is overseas, so COS traffic becomes China -> US -> China:

| Path | COS latency | COS throughput |
|---|---|---|
| via the overseas proxy | 2.23s | **54 KiB/s** |
| direct from `vm-0-8-ubuntu` (node netns) | 0.026s | **3.96 MB/s** |

~73x slower. Worse, it is not merely slow — **COS uploads through the proxy
fail outright once they are any real size.** Measured from the worker pod with
the app's own COS client (`put_object`):

| Upload | Result |
|---|---|
| 64 KiB | 13.3 KiB/s |
| 512 KiB | 8.2 KiB/s |
| 1.8 MB (the test reference video) | **FAILED** — `CosClientError('Connection aborted.', TimeoutError('The write operation timed out'))` after 131s |

Throughput *degrades* as the payload grows, so the proxy path cannot carry
reference videos at all. Small request/response traffic (Vertex AI JSON) is
fine over it — a real `generate_content` call returned in 6.6s.

The 1.8MB upload also blew nginx's 60s `proxy_read_timeout` (now raised to
600s in `frontend-vite/nginx.conf`), but that timeout was only masking this.

**The correct end state needs both halves:**

1. **COS direct** — fix pod egress on `vm-0-8-ubuntu` (the `ip rule` from
   "Two independent problems" above), so media moves at 3.96 MB/s.
2. **Google via the proxy** — keep `35-egress-proxy.yaml`, and then add
   `.myqcloud.com` to `NO_PROXY` so COS bypasses it. That is exactly what
   `deploy/docker-compose.dev.yml` already does
   (`NO_PROXY: ...,.myqcloud.com`).

Until (1) is done, `.myqcloud.com` must **stay out** of `NO_PROXY` — without
the `ip rule`, bypassing the proxy for COS means no COS at all. But note that
leaving it in the proxy path does not actually work either (uploads >~1MB
fail), so **(1) is required, not an optimisation**. There is no proxy-only
configuration of this app that works.

The model cache has the same problem: `faster-whisper large-v3` is ~3GB from
HuggingFace, which over this path is ~16 hours and would realistically fail
like the uploads do. With pod egress fixed (3.96 MB/s) it is ~13 minutes.

### Verified end to end

| Link | Result |
|---|---|
| nginx routing (`/api/`, SSE regex, upload cap) | 200 / 422 / 404 as expected |
| backend image | `/health` -> `{"status":"ok","redis":"ok","db":"ok"}` |
| worker image | arq up, 7 functions incl. `run_content_analysis` |
| COS real credentials | `put` -> `get` -> `delete`, content byte-identical |
| Vertex AI real call | `gemini-3.1-pro-preview` returned in 6.6s via the proxy |
| **full content-analysis flow** | **`completed` in 171.9s** — see below |
| SSE through nginx | `state_snapshot` arrived live (buffering off confirmed) |
| stability | 5/5 pods, 0 restarts, **no OOMKilled** |

The real run: an 8s previously-generated shot video was uploaded through the
real nginx -> backend -> COS, the worker transcribed it with faster-whisper
`small`, and Vertex produced the brief.

```
sample : transcribed | has_speech=True | transcript_len=108
  text : "You keep asking the cards why the physical chemistry is so intense,
          but the communication is completely dead"
brief  : 915 chars
keys   : niche_summary, sample_stats, hook_strategy, script_structure,
         do, dont, screenwriter_directives
```

Worker peaked at ~1004Mi resident with `small` (limit 2560Mi) and the node sat
at 70%. `large-v3` would add roughly another 1–1.5Gi on top of that, which is
why `ASR_MODEL` is set to `small` — see the comment in `10-configmap.yaml`.

## Memory risk — read this before deploying

The node has ~2.2Gi realistically free and the worker's request/limit is
**1Gi / 2.5Gi**. faster-whisper `large-v3` (int8, CPU) alone can hold ~1.5–2Gi
resident once a transcription job runs, and arq's `WORKER_POOL_SIZE=4` means
up to 4 concurrent jobs. **The worker may get OOMKilled**, especially if it
ever runs concurrently with a memory spike in the monitoring stack.

Observe:
```bash
kubectl top pod -n video-maker
kubectl -n video-maker describe pod -l component=worker   # look for "OOMKilled" in Last State
kubectl -n video-maker logs deploy/video-maker-worker --previous
```

Mitigation levers, in order of preference:
1. **Drop `ASR_MODEL`** in `10-configmap.yaml` from `large-v3` to `medium` or
   `small` (materially smaller resident memory, some accuracy loss) —
   requires clearing `video-maker-whisper-cache`'s old weights or accepting a
   one-time re-download on the new model name.
2. **Lower `WORKER_POOL_SIZE`** (in `10-configmap.yaml`) to reduce concurrent
   job memory, at the cost of throughput.
3. **Raise the node's RAM** (Tencent Cloud VM resize) — the only lever that
   doesn't trade off model quality or throughput.

## Update

```bash
kubectl -n video-maker rollout restart deploy/video-maker-backend
kubectl -n video-maker rollout restart deploy/video-maker-worker
kubectl -n video-maker rollout restart deploy/video-maker-frontend
kubectl -n video-maker rollout status  deploy/video-maker-backend
```

## Rollback

```bash
kubectl -n video-maker rollout undo deploy/video-maker-backend
```

## Teardown

```bash
kubectl delete namespace video-maker
```

**Note:** deleting the namespace deletes the PVCs (`video-maker-data`,
`video-maker-whisper-cache`) and, with them, the sqlite DB and the cached
whisper weights — not shared with the dev-compose `deploy_app-data` /
`deploy_app-storage` volumes, which are docker/podman volumes on the dev
host, not k8s resources. This deployment does not touch dev-compose state.

## Notes

- Media does **not** get a PVC — the app stores shot media in Tencent COS
  (`COS_REGION`/`COS_BUCKET` in `10-configmap.yaml`, `COS_SECRET_ID`/
  `COS_SECRET_KEY` in the Secret) and only uses ephemeral temp dirs locally.
- `vc-worker` (voice conversion) is intentionally not deployed — it's a
  heavier model not needed for this slice.
- CORS is enforced by FastAPI (`CORS_ORIGINS` in `10-configmap.yaml`) —
  update it once the real frontend hostname is decided (currently still the
  dev-compose localhost origins).
- Both `backend` and `worker` run `strategy: Recreate` and `replicas: 1`
  deliberately — they share a single sqlite file on one PVC; running two
  pods (even briefly, during a rolling update) risks concurrent writes.
