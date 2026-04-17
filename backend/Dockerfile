# Video Maker Backend Dockerfile - Single stage with pip
FROM python:3.12-slim-bookworm

# Configure Aliyun APT mirror
RUN rm -f /etc/apt/sources.list.d/* && \
    echo "deb http://mirrors.aliyun.com/debian/ bookworm main contrib non-free non-free-firmware" > /etc/apt/sources.list && \
    echo "deb http://mirrors.aliyun.com/debian-security/ bookworm-security main contrib non-free non-free-firmware" >> /etc/apt/sources.list && \
    echo "deb http://mirrors.aliyun.com/debian/ bookworm-updates main contrib non-free non-free-firmware" >> /etc/apt/sources.list

# Install runtime dependencies (ffmpeg for video processing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Set pip to use Tsinghua mirror
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/workspace \
    APP_ENV=production

# Set working directory
WORKDIR /workspace

# Copy and install dependencies first (for layer caching)
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    fastapi uvicorn sqlalchemy aiosqlite arq redis pydantic pydantic-settings \
    google-genai ffmpeg-python python-multipart sse-starlette python-json-logger

# Copy application code
COPY app/ ./app/
COPY worker/ ./worker/
COPY prompts/ ./prompts/

# Create directories
RUN mkdir -p /workspace/storage /workspace/logs

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Start application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
