#!/bin/bash
set -e

# Video Maker Build and Serve Script
# Builds frontend static files and starts nginx + backend

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}🚀 Video Maker - Build and Serve${NC}"
echo "=================================="

# Step 1: Build frontend
echo -e "\n${YELLOW}📦 Step 1: Building frontend...${NC}"
cd "${PROJECT_ROOT}/frontend"

# Create static export config
cat > next.config.js << 'EOF'
/** @type {import("next").NextConfig} */
const nextConfig = {
  output: "export",
  distDir: "dist",
  assetPrefix: ".",
  images: {
    unoptimized: true,
  },
};
module.exports = nextConfig;
EOF

# Install dependencies if needed
if [ ! -d "node_modules" ]; then
    echo "Installing dependencies..."
    npm ci
fi

# Build
npm run build

echo -e "${GREEN}✅ Frontend built successfully to ./frontend/dist/${NC}"

# Step 2: Start services
echo -e "\n${YELLOW}🐳 Step 2: Starting services with Docker Compose...${NC}"
cd "${PROJECT_ROOT}/.devcontainer"

# Create production docker-compose with local paths
cat > docker-compose.local.yml << EOF
version: '3.8'

services:
  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - ${PROJECT_ROOT}/frontend/dist:/usr/share/nginx/html:ro
    depends_on:
      - backend
    networks:
      - video-maker-network
    healthcheck:
      test: ["CMD", "wget", "-q", "--spider", "http://localhost/nginx-health"]
      interval: 10s
      timeout: 3s
      retries: 3

  backend:
    build:
      context: ${PROJECT_ROOT}/backend
      dockerfile: Dockerfile
    environment:
      - PYTHONUNBUFFERED=1
      - PYTHONPATH=/workspace
      - APP_ENV=production
      - REDIS_URL=redis://redis:6379
      - DATABASE_URL=sqlite+aiosqlite:///workspace/data/production.db
      - LOG_LEVEL=info
      - CORS_ORIGINS=http://localhost,http://127.0.0.1,http://nginx
    volumes:
      - backend_data:/workspace/data
      - backend_storage:/workspace/storage
      - backend_logs:/workspace/logs
    depends_on:
      - redis
    networks:
      - video-maker-network

  redis:
    image: redis:7-alpine
    volumes:
      - redis_data:/data
    networks:
      - video-maker-network

  worker:
    build:
      context: ${PROJECT_ROOT}/backend
      dockerfile: Dockerfile
    command: ["python", "-m", "arq", "worker.WorkerSettings"]
    environment:
      - PYTHONUNBUFFERED=1
      - PYTHONPATH=/workspace
      - APP_ENV=production
      - REDIS_URL=redis://redis:6379
      - DATABASE_URL=sqlite+aiosqlite:///workspace/data/production.db
      - LOG_LEVEL=info
    volumes:
      - backend_data:/workspace/data
      - backend_storage:/workspace/storage
      - backend_logs:/workspace/logs
    depends_on:
      - redis
      - backend
    networks:
      - video-maker-network

volumes:
  backend_data:
  backend_storage:
  backend_logs:
  redis_data:

networks:
  video-maker-network:
    driver: bridge
EOF

# Start services
docker-compose -f docker-compose.local.yml up --build -d

echo -e "${GREEN}✅ Services started!${NC}"
echo ""
echo -e "${GREEN}🌐 Access URLs:${NC}"
echo "  - Frontend: http://localhost"
echo "  - Backend API: http://localhost/api/"
echo "  - API Docs: http://localhost/api/docs"
echo ""
echo -e "${YELLOW}📋 Useful commands:${NC}"
echo "  - View logs: docker-compose -f .devcontainer/docker-compose.local.yml logs -f"
echo "  - Stop: docker-compose -f .devcontainer/docker-compose.local.yml down"
echo "  - Restart: docker-compose -f .devcontainer/docker-compose.local.yml restart"
