# FastAPI backend image.
#
# Build context is `backend/`:
#     podman build -t <tag> -f backend/Dockerfile backend/
#
# Dependencies come from pyproject.toml + uv.lock (the single source of truth)
# rather than a hand-maintained pip list — the old hardcoded list had drifted
# and was missing openai / httpx / scikit-image / langfuse / fastmcp /
# cos-python-sdk-v5, all imported by current app code.
#
# WORKDIR is /app (not /workspace) so the shared sqlite PVC that
# deploy/k8s/40-backend.yaml mounts at /app/data lines up with
# DATABASE_URL=sqlite+aiosqlite:////app/data/dev.db — and with the worker,
# which mounts the same PVC at the same path.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN rm -f /etc/apt/sources.list.d/debian.sources && \
    echo "deb https://mirrors.aliyun.com/debian/ bookworm main contrib non-free non-free-firmware" > /etc/apt/sources.list && \
    echo "deb https://mirrors.aliyun.com/debian/ bookworm-updates main contrib non-free non-free-firmware" >> /etc/apt/sources.list && \
    echo "deb https://mirrors.aliyun.com/debian-security bookworm-security main contrib non-free non-free-firmware" >> /etc/apt/sources.list && \
    apt-get update -qq \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    APP_ENV=production \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

# Dependencies first, for layer caching. No `asr` group — faster-whisper is
# worker-only (see deploy/Dockerfile.worker).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Application source.
COPY app/ ./app/
COPY worker/ ./worker/
COPY prompts/ ./prompts/
COPY mcp_server/ ./mcp_server/

RUN mkdir -p /app/data /app/storage /app/logs

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# `uvicorn` resolves to /app/.venv/bin/uvicorn via PATH above.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
