# Project-Wide Development Rules

## Secrets Management (K8s-style)

**All secrets live in `secrets.yml` (gitignored). Never hardcode credentials in code or config.**

### Workflow

1. Copy template and fill in values (one-time setup):
   ```bash
   cp deploy/secrets.yml.example secrets.yml
   # Edit secrets.yml with real credentials
   ```

2. Apply secrets (writes each YAML key → `secrets/<key>` file):
   ```bash
   make secrets
   ```

3. `make dev` runs `make secrets` automatically before starting containers.

### Format

`secrets.yml` is a flat YAML file — each key becomes a file under `secrets/`:

```yaml
gemini_api_key: AIzaSy...
```

Written to `secrets/gemini_api_key`, mounted in containers at `/run/secrets/gemini_api_key`.

### Compose wiring

Secrets are declared in `docker-compose.dev.yml` using the compose `secrets:` directive:

```yaml
secrets:
  gemini_api_key:
    file: ../secrets/gemini_api_key   # written by: make secrets

services:
  backend:
    secrets:
      - gemini_api_key
    command: >
      sh -c "export GEMINI_API_KEY=$(cat /run/secrets/gemini_api_key) && uvicorn ..."
```

### Rules

- **Never mount the `secrets/` directory as a volume** — use compose `secrets:` instead
- **Never commit `secrets.yml` or `secrets/`** — both are gitignored
- **Add new secrets** to `deploy/secrets.yml.example` (with placeholder values) AND to `docker-compose.dev.yml`
- To add a new secret: add key to `deploy/secrets.yml.example`, add `secrets:` entry in compose, reference in service `secrets:` and `environment:`



## Shot 素材文件变更审计

**任何修改 shot 素材文件（视频、音频、帧图片）的代码变更，必须审计所有引用这些文件的下游代码路径，确认不会读到过期文件。**

### 背景

每个 shot 目录下存在多个版本的素材文件，它们之间有依赖关系：

| 文件 | 用途 | 产生时机 |
|------|------|----------|
| `output.mp4` | 当前视频（`shot.video_path`） | 生成 / 裁剪 / VC / 还原 |
| `output_original.mp4` | 裁剪前原始备份 | 首次裁剪时从 `output.mp4` 重命名 |
| `output_pre_vc.mp4` | VC 前备份 | 首次 voice clone 时从 `output.mp4` 复制 |
| `last_frame.png` | 最后一帧 | 生成 / 裁剪 / VC / 还原时重新提取 |
| `last_frame_pre_cc.png` | 角色校准前备份 | 首次 CC 时复制 |
| `audio_original.wav` | VC 前原始音频 | VC 时从视频提取 |

### 审计规则

当你的代码变更涉及以下操作时，**必须启动审计**：

1. **修改了任何写入 / 重命名 / 删除素材文件的逻辑**（裁剪、还原、VC、CC、生成）
2. **新增了读取素材文件的代码路径**（导出、合并、预览等）
3. **改变了素材文件的命名或存储位置**

### 审计检查清单

- [ ] 所有下游读取方是否通过 `shot.video_path`（DB 字段）或 `shot_output_path()` 获取路径，而非硬编码文件名
- [ ] 备份文件（`output_original.mp4`、`output_pre_vc.mp4`、`last_frame_pre_cc.png`）在素材变更后是否需要清除或更新
- [ ] 相关的 status 字段（`vc_status`、`cc_status`）是否需要重置
- [ ] `get_original_video_for_audio()` 的优先级链是否仍然正确
- [ ] 新代码路径是否会意外读到过期的备份文件

### 示例：裁剪操作需要清理的关联文件

```python
# 裁剪/还原后必须清理：
# 1. 重新提取 last_frame
# 2. 删除 pre-CC last_frame 备份 + 重置 cc_status
# 3. 删除 pre-VC video 备份 + 重置 vc_status
```


## 音视频改动：必须用「解码内容」验证，禁止用代理信号验证

**任何影响用户实际听到/看到的音视频的改动（音色校准、裁剪、拼接、音轨替换、静音、预览、导出），验证时必须比对「解码后的内容」本身，禁止只检查代理信号就宣称修好。**

### 背景（这条规则的由来）

修「配音/原音」切换时，用「`play()` 被调用了、`muted`/`paused` 标志位变了、音频文件是 48kHz 立体声、时长对上了」来证明修复有效——这些全是**代理信号**，没有一个能证明**用户实际听到的声音真的换了**。「切换了还是一样」一句就戳穿：机制对不代表听感对。真正能下结论的，是把两个音源解码成 PCM、用 md5 / 逐样本 RMS 差比对内容。

### 代理信号（不足以验证，禁止据此宣称修好）

- 某个函数被调用了（`play()`、`build_effective_clip()` 跑过了）
- DOM / 状态标志位（`muted`、`paused`、`useVc`、`vc_status=done`）
- 容器元数据（采样率、声道数、时长、码率、`ffprobe` 的 stream 字段）
- 文件存在、URL 返回 200、文件大小非零
- Mock / spy 断言（`toHaveBeenCalled`）

### 内容验证（必须做到）

- 把相关音频**解码成统一格式的 PCM**（如 `ffmpeg -i x -vn -ar 16000 -ac 1 -f s16le`），再 md5 / 逐样本 RMS 差比对。
- 断言的是「**该相同的相同、该不同的不同**」：
  - 改动**应当**改变内容（如切到校准音）→ 断言新内容与旧内容 RMS 差显著。
  - 改动**不应**改变内容（如裁剪只截长度、非破坏式操作不动原文件）→ 断言相关片段 md5 一致 / RMS 差≈0。
- 视频同理：抽帧比对像素（`extract_frame_at` + md5），而非只看分辨率/帧数。
- e2e/集成层验证「用户实际得到的产物」：驱动真实端点产出真实文件，再解码内容断言——不要断言你自己喂进去的值（见下方 E2E 规则）。

### 素材血缘：改动前先确认「原始档案」是否被动过

怀疑「原音频不见了 / 被替换了」这类问题时，先查**文件血缘**再下结论：

- `ls --time-style` 看 mtime 时序：`output.mp4` 若在 VC/CC 之后**未被改动**，说明是非破坏式，原始内容仍在原文件里。
- 用内容比对确认 `shot.video_path` 里的音轨到底是原始音（== `audio_in`）还是克隆音（== `audio_vc`），不要靠文件名猜。
- VC 是**非破坏式**：只生成独立的 `audio_vc_*.wav` + 设 `vc_audio_path`，**不覆盖 `output.mp4`**；`voice-revert` 只把 `vc_audio_path=None` 即可还原。所以「原音」始终躺在 `output.mp4` 里。


## Google GenAI — 必须使用 Vertex AI

**所有后端对接 `google.genai` 的代码必须使用 `vertexai=True`，通过 service account 认证，禁止使用 API key。**

```python
# ✓ 正确
client = genai.Client(vertexai=True, project=settings.project, location=settings.location)

# ✗ 禁止
client = genai.Client(api_key=settings.gemini_api_key)
```

## E2E Tests — NEVER fake the data or flow under test

**E2E (Playwright) tests MUST exercise the REAL backend, REAL DB, REAL serialization, and the REAL endpoint flow being tested. NEVER fake them.**

A faked e2e gives false confidence: a test that `route.fulfill`s a hardcoded
project/shot JSON (with fields like `trim_end_sec`/`vc_audio_url` pre-baked) is
NOT testing the feature — it only tests that the frontend renders the values you
handed it. Such a test passes even when the real `POST /trim → DB → serializer →
player` chain is broken. This actually happened on the non-destructive editing
work: mocked e2e were green while real trimming didn't apply. Do not do this.

**Rules:**
- Drive the real UI against the running stack; let requests hit the real backend.
- Seed test data the REAL way, maximizing chain realism while NEVER calling an
  LLM/model. Preferred: REUSE an existing already-generated asset — copy a real
  `output_<ts>_<uuid>.mp4` produced by a past generation into a FRESH, isolated
  test project's shot dir, and insert the matching `Shot` row (status=completed)
  + project (status=shot_review) directly in the real DB — e.g. via `podman exec`
  into the backend container. This gives a real video, real row, real
  serialization, and real endpoints, with only the billed generation skipped.
  Use an isolated test project (not a user's real project) so the test can mutate
  freely and be deleted in teardown.
- Assert on the REAL post-action state: after clicking trim, the real `/trim`
  ran, the real `GET /api/projects/{id}` reflects `trim_frames`/`trim_end_sec`,
  and the player clamps. Never assert on values you injected via a route mock.

**The ONLY thing you may stub is the actual AI MODEL invocation, purely to avoid billing** — and even then, stub at the model/worker boundary, not by substituting whole API responses. You may `route.fulfill` an AI-*triggering* endpoint ONLY to stop a click from kicking off a billed worker (return its real `202` shape); you may NOT use it to feed the data the test asserts on.

AI-triggering endpoints (safe to short-circuit to their real queued response):
- `POST /api/projects/{id}/start`, `/approve-script`, `/regenerate-script`,
  `/regenerate-shots`, `/export`
- `POST /api/projects/{id}/shots/{shot_id}/generate-tail-frame`, `/confirm-tail-frame`, `/voice-convert`

```typescript
// OK: stop a billed worker from running, return its real shape
await page.route('**/api/projects/*/start', (route) =>
  route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) }))

// FORBIDDEN: faking the data/flow under test
// await page.route('**/api/projects/*', (route) => route.fulfill({ body: JSON.stringify(FAKE_PROJECT) }))
```

## No Hardcoded Absolute Paths

**Never write hardcoded absolute paths in any file (code, config, scripts).**

Use relative paths or dynamic resolution instead:

```typescript
// TypeScript/JavaScript — relative to current file
import path from 'path'
const file = path.resolve(__dirname, '../fixtures/test.jpg')   // ✓
const file = '/home/wayne/tools/video_maker/tests/fixtures/test.jpg'  // ✗

// Node execSync — use cwd option instead of absolute paths
execSync('uv run ...', { cwd: path.resolve(__dirname, '../../backend') })  // ✓
execSync('cd /home/wayne/... && uv run ...')  // ✗
```

```makefile
# Makefile — use $(PWD) or relative paths
-v $(PWD)/frontend-vite/dist:/usr/share/nginx/html:ro   # ✓
-v /home/wayne/tools/video_maker/frontend-vite/dist:...  # ✗
```

```python
# Python — use pathlib relative to __file__
from pathlib import Path
BASE = Path(__file__).parent.parent  # ✓
BASE = Path('/home/wayne/tools/video_maker')  # ✗
```

## Always Run Python via Podman Compose

**Never run Python directly with `python`, `python3`, or `source .venv/bin/activate`.**

All backend services (backend, worker, redis) are defined in `deploy/docker-compose.dev.yml` and started with:

```bash
# Start full dev stack (backend + worker + redis)
podman compose -f deploy/docker-compose.dev.yml up -d

# Or via Makefile targets
make dev          # compose up + vite frontend
make dev-backend  # compose up (no frontend)
make dev-worker   # start worker only
make dev-logs     # follow logs
make dev-stop     # compose down + stop vite
```

**Never** add raw `podman run` commands for Python services — put them in the compose file instead. For one-off scripts during development:

```bash
podman run --rm --network host \
    -v $(PWD)/backend:/app:z -w /app \
    -e DATABASE_URL=sqlite+aiosqlite:////app/data/dev.db \
    ghcr.io/astral-sh/uv:python3.12-bookworm-slim \
    uv run --project . python some_script.py
```

Rules for compose services and one-off containers:
- Image: `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`
- Mount backend source as `/app` with `:z` (SELinux)
- DB path inside container: `sqlite+aiosqlite:////app/data/dev.db`
- Proxy for Google APIs: `HTTPS_PROXY: http://host.containers.internal:10809`

## Python Tooling

**Always use `pyproject.toml` to manage Python packages. Always use `uv` to run Python scripts.**

When running Python scripts or one-liners that need backend dependencies, explicitly specify the project:

```bash
# Run a script
uv run --project backend python some_script.py

# One-liner
uv run --project backend python -c "import sqlalchemy; print(sqlalchemy.__version__)"
```

**Never** call `python` / `python3` directly. **Never** use `pip install`.

To add a new package, add it to `backend/pyproject.toml` then run:

```bash
uv sync --project backend
```

## Local Deploy (worktree stack switching)

There is **one shared compose project `deploy`** for the whole repo. Every worktree's
`deploy/docker-compose.dev.yml` uses the same fixed ports and the same named volumes, so only
**one** stack runs at a time and **all worktrees share the same DB + media storage**:

| Service | Port | Notes |
|---------|------|-------|
| backend (FastAPI) | `8002` | `--reload`; docs at `http://localhost:8002/docs` |
| frontend (Vite) | `4000` | `http://localhost:4000` |
| redis | `6381` | |
| worker / vc-worker | — | ARQ |

Shared named volumes (`deploy_app-data` = sqlite DB, `deploy_app-storage` = media) persist across
worktrees. Source/secrets are bind-mounted **from whichever worktree you run compose in** — so
"deploying a worktree" = pointing the shared stack at that worktree.

### To run a specific worktree's code locally

```bash
cd <this-worktree>

# 1. Copy ALL gitignored config from a worktree that has it (the stack won't start without it).
#    Copy completely — a partial copy once dropped the Langfuse keys.
SRC=../<some-worktree-with-config>
cp -a "$SRC/deploy/secrets"     deploy/secrets        # all of: gcp-sa.json, gemini/veo/kie/deepseek_api_key,
cp -a "$SRC/deploy/secrets.yml" deploy/secrets.yml    #          langfuse_public_key, langfuse_secret_key, vertex*
cp -a "$SRC/deploy/config.env"  deploy/config.env     # must contain LANGFUSE_ENABLED / LANGFUSE_HOST
#    (deploy/config.yml is checked in and identical across worktrees; verify config.env matches it)

# 2. Frontend needs node_modules ON THE HOST (no node_modules volume; mount is ../frontend-vite:/app):
( cd frontend-vite && npm ci )

# 3. Switch the shared stack onto this worktree (recreates containers; shared DB/storage preserved):
podman compose -f deploy/docker-compose.dev.yml up -d
```

Verify: `curl -s localhost:8002/openapi.json | grep <new-route>` and `curl -sI localhost:4000`.
Confirm the mount switched: `podman inspect video-maker-backend-dev --format '{{range .Mounts}}{{if eq .Destination "/app"}}{{.Source}}{{end}}{{end}}'`.

### After editing backend code

`--reload` picks up edits in place, but if it doesn't:

```bash
podman restart video-maker-backend-dev video-maker-worker-dev
```

### Notes / gotchas

- **改了 `deploy/config.env` 必须 `down` + `up`，`restart` 不生效。** env_file 在容器
  **创建**时读取，podman-compose 还会缓存 spec：实测 `podman restart`、
  `podman compose up -d <service>`、甚至先 `podman rm -f` 再 `up -d <service>`，容器拿到的
  都还是旧值，且**不报任何错**——表现为「我改了开关，但一点反应都没有」。只有
  `podman compose -f deploy/docker-compose.dev.yml down && ... up -d` 才会重新读。
  验证方式：`podman exec video-maker-backend-dev sh -c 'echo $AUTH_ENFORCED'`，
  别只看文件内容。（k8s 侧不同：改 ConfigMap 后 `kubectl rollout restart` 会重建 Pod。）
- **A backend-only change does NOT alter the UI.** If the frontend looks unchanged after deploy, check
  `git diff <base>..HEAD -- frontend-vite` — an empty diff means there is no frontend code to show, so no
  amount of container rebuilding will change the page. (Rebuild only matters for `Dockerfile.*` / dep changes.)
- Switching the stack to a worktree **stops** whatever worktree it was previously serving; reverse by
  running compose from that other worktree.
- Missing host `frontend-vite/node_modules` ⇒ frontend container crash-loops with `vite: not found`.
