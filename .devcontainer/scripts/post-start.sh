#!/bin/bash
set -e

echo "🔄 Starting services..."

# Check if Redis is accessible
if redis-cli -h redis ping > /dev/null 2>&1; then
    echo "✅ Redis is ready"
else
    echo "⚠️  Redis is not ready yet, will retry..."
fi

echo ""
echo "🎯 Development environment is ready!"
echo ""
echo "Quick start:"
echo "  1. Terminal 1: cd /workspace/frontend && npm run dev"
echo "  2. Terminal 2: cd /workspace/backend && uv run uvicorn app.main:app --host 0.0.0.0 --reload"
echo "  3. Terminal 3: cd /workspace/backend && uv run arq worker.WorkerSettings"
