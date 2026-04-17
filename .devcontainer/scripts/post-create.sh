#!/bin/bash
set -e

echo "🚀 Setting up Video Maker development environment..."

# Setup Frontend
echo "📦 Installing frontend dependencies..."
cd /workspace/frontend
npm install

# Setup Backend
echo "🐍 Setting up Python environment..."
cd /workspace/backend
if [ -f "uv.lock" ]; then
    uv sync --frozen
else
    uv pip install -e ".[dev]"
fi

# Create necessary directories
mkdir -p /workspace/backend/data
mkdir -p /workspace/backend/storage
mkdir -p /workspace/backend/logs

echo "✅ Setup complete!"
echo ""
echo "Available commands:"
echo "  Frontend: cd /workspace/frontend && npm run dev"
echo "  Backend:  cd /workspace/backend && uv run uvicorn app.main:app --reload"
echo "  Worker:   cd /workspace/backend && uv run arq worker.WorkerSettings"
echo ""
echo "Services:"
echo "  Frontend: http://localhost:3000"
echo "  Backend:  http://localhost:8000"
echo "  API Docs: http://localhost:8000/docs"
echo "  Redis:    localhost:6379"
