# ---------------------------------------------
# Video Maker - Deployment Makefile
# Requires: podman/docker, npm, uv (Python)
# ---------------------------------------------

# Use bash and ensure PATH includes custom directories
SHELL := /bin/bash
export PATH := $(PATH):/ultra_sandbox/bin

# ---- Configuration (override via env vars) ----
REMOTE        ?= ubuntu@tencent
REMOTE_DIR    ?= /home/ubuntu/video_maker

# Container registry config
REGISTRY      ?= ghcr.io
NAMESPACE     ?= $(shell echo $(USER) | tr '[:upper:]' '[:lower:]')
FRONTEND_IMAGE ?= video-maker-frontend
BACKEND_IMAGE  ?= video-maker-backend
COSYVOICE_IMAGE  ?= video-maker-cosyvoice-vc
VC_WORKER_IMAGE  ?= video-maker-vc-worker
TAG           ?= latest

FRONTEND_FULL_IMAGE  = $(REGISTRY)/$(NAMESPACE)/$(FRONTEND_IMAGE):$(TAG)
BACKEND_FULL_IMAGE   = $(REGISTRY)/$(NAMESPACE)/$(BACKEND_IMAGE):$(TAG)
COSYVOICE_FULL_IMAGE  = $(REGISTRY)/$(NAMESPACE)/$(COSYVOICE_IMAGE):$(TAG)
VC_WORKER_FULL_IMAGE  = $(REGISTRY)/$(NAMESPACE)/$(VC_WORKER_IMAGE):$(TAG)

# Local ports
FRONTEND_PORT ?= 4000
BACKEND_PORT  ?= 8002
REDIS_PORT    ?= 6379

# Deploy directory (compose, config, secrets)
DEPLOY_DIR    ?= deploy
DEV_COMPOSE   ?= $(DEPLOY_DIR)/docker-compose.dev.yml

# ---------------------------------------------
.PHONY: help dev build push deploy deploy-frontend deploy-backend \
        dev-frontend dev-backend dev-redis dev-stop dev-worker \
        prod-up prod-down prod-logs prod-ps \
        login build-frontend build-backend \
        sync remote-deploy clean \
        test test-ui \
        secrets config \
        get-script

# ---- Help -----------------------------------
help:
	@echo ""
	@echo "Video Maker - Deployment Commands"
	@echo "---------------------------------------------"
	@echo ""
	@echo "[Development Environment]"
	@echo "  make dev              Vite dev server (HMR) + backend — two ports, Vite proxies /api"
	@echo "  make dev-nginx        Build frontend → nginx on :4000, no CORS (no HMR)"
	@echo "  make dev-frontend     Start frontend Vite dev server only"
	@echo "  make dev-backend      Start backend dev server only"
	@echo "  make dev-worker       Start background worker"
	@echo "  make dev-redis        Start local Redis"
	@echo "  make dev-stop         Stop all dev services"
	@echo ""
	@echo "[Build Images]"
	@echo "  make build            Build all images"
	@echo "  make build-frontend   Build frontend image only"
	@echo "  make build-backend    Build backend image only"
	@echo ""
	@echo "[Push Images]"
	@echo "  make login            Login to container registry"
	@echo "  make push             Push all images"
	@echo ""
	@echo "[Production Deployment]"
	@echo "  make prod-up          Start production environment"
	@echo "  make prod-down        Stop production environment"
	@echo "  make prod-logs        View production logs"
	@echo "  make prod-ps          View production container status"
	@echo ""
	@echo "[Remote Deployment]"
	@echo "  make sync             Sync code to remote server"
	@echo "  make remote-deploy    Deploy on remote server"
	@echo ""
	@echo "[One-Click Deploy]"
	@echo "  make deploy           Full deploy: build -> push -> prod-up"
	@echo ""
	@echo "[Testing]"
	@echo "  make test             Run Playwright e2e tests (dev stack must be up)"
	@echo "  make test-ui          Run tests with interactive Playwright UI"
	@echo ""
	@echo "[Config & Secrets]"
	@echo "  make config           Write config.yml → config.env (run after editing config.yml)"
	@echo "  make secrets          Write secrets.yml → secrets/ files (run after editing secrets.yml)"
	@echo ""
	@echo "[Cleanup]"
	@echo "  make clean            Clean build artifacts and containers"
	@echo ""

# =============================================
# Development Environment
# =============================================

# Write deploy/config.yml → deploy/config.env (K8s-style ConfigMap)
config:
	@test -f $(DEPLOY_DIR)/config.yml || (echo "ERROR: $(DEPLOY_DIR)/config.yml not found." && exit 1)
	@python3 -c "import yaml; s=yaml.safe_load(open('$(DEPLOY_DIR)/config.yml')); lines=[f'{k}={v}' for k,v in s.items()]; open('$(DEPLOY_DIR)/config.env','w').write('\n'.join(lines)+'\n'); print('Config written to $(DEPLOY_DIR)/config.env ('+str(len(lines))+' vars)')"

# Write deploy/secrets.yml → deploy/secrets/ files (K8s-style Secret)
secrets:
	@test -f $(DEPLOY_DIR)/secrets.yml || (echo "ERROR: $(DEPLOY_DIR)/secrets.yml not found — copy $(DEPLOY_DIR)/secrets.yml.example and fill in values." && exit 1)
	@mkdir -p $(DEPLOY_DIR)/secrets
	@python3 -c "import yaml,pathlib; s=yaml.safe_load(open('$(DEPLOY_DIR)/secrets.yml')); d=pathlib.Path('$(DEPLOY_DIR)/secrets'); [((d/k).write_text(str(v).strip()), print('  wrote $(DEPLOY_DIR)/secrets/'+k)) for k,v in s.items()]; print('Secrets applied.')"

# Start full dev environment (all services via compose)
dev: config secrets dev-stop
	cd frontend-vite && npm install --prefer-offline 2>/dev/null || npm install
	podman compose -f $(DEV_COMPOSE) up -d
	@echo ""
	@echo "Dev environment started — http://localhost:4000"
	@echo "Logs: make dev-logs"

# Start frontend only
dev-frontend:
	podman compose -f $(DEV_COMPOSE) up -d frontend

# Start backend stack only (no frontend)
dev-backend:
	@echo "Starting backend stack..."
	podman compose -f $(DEV_COMPOSE) up -d

# Start worker only
dev-worker:
	@echo "Starting worker..."
	podman compose -f $(DEV_COMPOSE) up -d worker

# Start Redis only
dev-redis:
	@echo "Starting Redis..."
	podman compose -f $(DEV_COMPOSE) up -d redis

# View dev logs
dev-logs:
	podman compose -f $(DEV_COMPOSE) logs --tail 100

# Build frontend and serve via nginx + backend on same port (no CORS, no HMR)
# All traffic: http://localhost:$(FRONTEND_PORT)
dev-nginx: dev-stop dev-backend
	@echo "Building frontend..."
	cd frontend-vite && npm run build
	@echo "Starting nginx (static build + /api proxy → backend:$(BACKEND_PORT))..."
	podman run -d --rm --name video-maker-dev-nginx \
		--network host \
		-v $(PWD)/frontend-vite/dist:/usr/share/nginx/html:ro \
		-v $(PWD)/frontend-vite/nginx-dev.conf:/etc/nginx/conf.d/default.conf:ro \
		nginx:alpine
	@echo ""
	@echo "Dev (nginx) running at http://localhost:$(FRONTEND_PORT)"
	@echo "  Frontend: static build (no HMR)"
	@echo "  API:      proxied to localhost:$(BACKEND_PORT)"
	@echo "  No CORS issues — everything on same origin"

# Stop dev services
dev-stop:
	@echo "Stopping development services..."
	-pkill -f "vite" 2>/dev/null || true
	-podman compose -f $(DEV_COMPOSE) down 2>/dev/null || true
	-podman rm -f video-maker-dev-nginx 2>/dev/null || true
	@echo "Development services stopped"

# =============================================
# Build Images
# =============================================

# Build all images
build: build-frontend build-backend build-cosyvoice

# Build frontend image
build-frontend:
	@echo "Building frontend image..."
	cd frontend-vite && npm run build
	podman build \
		--platform linux/amd64 \
		-t $(FRONTEND_FULL_IMAGE) \
		-f frontend-vite/Dockerfile \
		./frontend-vite

# Build backend image
build-backend:
	@echo "Building backend image..."
	podman build \
		--platform linux/amd64 \
		-t $(BACKEND_FULL_IMAGE) \
		-f backend/Dockerfile \
		./backend

# Build CosyVoice VC service image (standalone HTTP service, optional)
build-cosyvoice:
	@echo "Building CosyVoice VC image..."
	podman build \
		--platform linux/amd64 \
		-t $(COSYVOICE_FULL_IMAGE) \
		-f cosyvoice-vc/Dockerfile \
		.

# Build VC worker image (vc2 + ONNX models baked in)
build-vc-worker:
	@echo "Building VC worker image..."
	podman build \
		--platform linux/amd64 \
		-t $(VC_WORKER_FULL_IMAGE) \
		-f deploy/Dockerfile.vc-worker \
		.

# =============================================
# Push Images
# =============================================

# Login to registry
login:
	@echo "Logging in to $(REGISTRY)..."
	podman login $(REGISTRY)

# Push all images
push: login
	@echo "Pushing frontend image..."
	podman push $(FRONTEND_FULL_IMAGE)
	@echo "Pushing backend image..."
	podman push $(BACKEND_FULL_IMAGE)

# =============================================
# Production Deployment (Local Podman/Docker)
# =============================================

# Start production environment
prod-up:
	@echo "Starting production environment..."
	cd .devcontainer && \
		FRONTEND_IMAGE=$(FRONTEND_FULL_IMAGE) \
		BACKEND_IMAGE=$(BACKEND_FULL_IMAGE) \
		podman compose -f docker-compose.simple.yml up -d
	@echo ""
	@echo "Production environment started"
	@echo "  Frontend: http://localhost"
	@echo "  API:      http://localhost/api/"
	@echo "  Docs:     http://localhost/api/docs"

# Stop production environment
prod-down:
	@echo "Stopping production environment..."
	cd .devcontainer && podman compose -f docker-compose.simple.yml down

# View production logs
prod-logs:
	@echo "Production environment logs..."
	cd .devcontainer && podman compose -f docker-compose.simple.yml logs -f

# View production container status
prod-ps:
	@echo "Production container status..."
	cd .devcontainer && podman compose -f docker-compose.simple.yml ps

# =============================================
# Remote Deployment
# =============================================

# Sync code to remote server
sync:
	@echo "Syncing code to $(REMOTE)..."
	rsync -avz --delete \
		--exclude='frontend/node_modules/' \
		--exclude='frontend/.next/' \
		--exclude='backend/.venv/' \
		--exclude='backend/__pycache__/' \
		--exclude='*.pyc' \
		--exclude='.git/' \
		--exclude='backend/data/' \
		--exclude='backend/storage/' \
		./ $(REMOTE):$(REMOTE_DIR)/

# Deploy on remote server
remote-deploy: sync
	@echo "Deploying on remote server..."
	ssh $(REMOTE) "cd $(REMOTE_DIR) && make prod-up"

# =============================================
# One-Click Deploy
# =============================================

# Full deployment pipeline
deploy: build push prod-up
	@echo "Deployment complete!"
	@echo ""
	@echo "Access URLs:"
	@echo "  Frontend: http://localhost"
	@echo "  API:      http://localhost/api/"

# Frontend only deploy
deploy-frontend: build-frontend push prod-up
	@echo "Frontend deployment complete!"

# Backend only deploy
deploy-backend: build-backend push prod-up
	@echo "Backend deployment complete!"

# =============================================
# Cleanup
# =============================================

# =============================================
# Testing
# =============================================

# Run Playwright e2e tests (requires dev stack running: make dev)
test:
	npx playwright@1.41.0 test --config=playwright.config.ts

# Run tests with interactive UI mode
test-ui:
	npx playwright@1.41.0 test --config=playwright.config.ts --ui

# =============================================
# AI Tools
# =============================================

# Fetch the latest generated script (pass ID=<project_id> or JSON=1 for raw JSON)
get-script:
	@uv run --project backend python ai_tools/get_latest_script.py \
		$(if $(ID),--id $(ID),) \
		$(if $(JSON),--json,) \
		$(if $(SAVE),--save $(SAVE),)

clean:
	@echo "Cleaning build artifacts..."
	cd frontend-vite && rm -rf dist node_modules
	cd backend && rm -rf .venv __pycache__ .pytest_cache
	cd .devcontainer && podman compose -f docker-compose.simple.yml down -v 2>/dev/null || true
	podman rmi $(FRONTEND_FULL_IMAGE) $(BACKEND_FULL_IMAGE) 2>/dev/null || true
	@echo "Cleanup complete"
