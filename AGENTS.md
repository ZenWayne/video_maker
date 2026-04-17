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



## Playwright Tests — AI Endpoint Mocking

**Always mock AI-triggering backend endpoints in Playwright tests to avoid billing.**

Endpoints that trigger AI workers (mock with `route.fulfill`):
- `POST /api/projects/{id}/start`
- `POST /api/projects/{id}/approve-script`
- `POST /api/projects/{id}/regenerate-script`
- `POST /api/projects/{id}/regenerate-shots`
- `POST /api/projects/{id}/export`

```typescript
await page.route('**/api/projects/*/start', async (route) => {
  await route.fulfill({ status: 202, body: JSON.stringify({ status: 'queued' }) })
})
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
