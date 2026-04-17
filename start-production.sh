#!/bin/bash
set -e

# Video Maker Production Startup Script
# Runs frontend and backend directly without Docker

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "🚀 Starting Video Maker (Production Mode)"
echo "=========================================="

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Function to cleanup processes on exit
cleanup() {
    echo -e "\n${YELLOW}Shutting down services...${NC}"
    pkill -f "vite" 2>/dev/null || true
    pkill -f "uvicorn app.main" 2>/dev/null || true
    pkill -f "arq worker" 2>/dev/null || true
    exit 0
}
trap cleanup INT TERM

# Start Backend
echo -e "${YELLOW}Starting Backend...${NC}"
cd "$SCRIPT_DIR/backend"
source .venv/bin/activate
export APP_ENV=production
export REDIS_URL=redis://localhost:6379
export DATABASE_URL=sqlite+aiosqlite:///$SCRIPT_DIR/backend/data/production.db
export CORS_ORIGINS=http://localhost:4000,http://127.0.0.1:4000

# Start Redis if not running
if ! redis-cli ping > /dev/null 2>&1; then
    echo "Starting Redis..."
    redis-server --daemonize yes --port 6379
fi

# Start Worker in background
nohup python -m arq worker.WorkerSettings > "$SCRIPT_DIR/worker.log" 2>&1 &
echo "Worker started (PID: $!)"

# Start Backend API in background
nohup uvicorn app.main:app --host 0.0.0.0 --port 8002 > "$SCRIPT_DIR/backend.log" 2>&1 &
BACKEND_PID=$!
echo "Backend started (PID: $BACKEND_PID)"

# Wait for backend to be ready
sleep 3

# Start Frontend (Vite)
echo -e "${YELLOW}Starting Frontend (Vite)...${NC}"
cd "$SCRIPT_DIR/frontend-vite"

# Build if needed
if [ ! -d "dist" ]; then
    echo "Building frontend..."
    npm run build
fi

# Serve built files
nohup npm run preview -- --port 4000 --host > "$SCRIPT_DIR/frontend.log" 2>&1 &
FRONTEND_PID=$!
echo "Frontend started (PID: $FRONTEND_PID)"

echo ""
echo -e "${GREEN}✅ All services started!${NC}"
echo ""
echo "Access URLs:"
echo "  - Frontend: http://localhost:4000"
echo "  - Backend:  http://localhost:8002"
echo "  - API Docs: http://localhost:8002/docs"
echo ""
echo "Logs:"
echo "  - Backend:  tail -f $SCRIPT_DIR/backend.log"
echo "  - Frontend: tail -f $SCRIPT_DIR/frontend.log"
echo "  - Worker:   tail -f $SCRIPT_DIR/worker.log"
echo ""
echo "Press Ctrl+C to stop all services"

# Keep script running
wait
