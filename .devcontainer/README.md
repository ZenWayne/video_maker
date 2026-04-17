# Dev Container 配置

此目录包含 Video Maker 项目的完整开发和生产部署配置。

## 目录结构

```
.devcontainer/
├── devcontainer.json              # VS Code Dev Container 配置
├── docker-compose.yml             # 开发环境（前端+后端+Redis+Worker）
├── docker-compose.simple.yml      # 生产环境（Nginx+后端+Redis+Worker）
├── docker-compose.prod.yml        # 完整生产编排
├── docker-compose.override.yml    # Docker Compose 覆盖配置
├── podman-compose.yml             # Podman 编排配置
├── nginx.conf                     # Nginx 反向代理配置
├── Dockerfile                     # 前端开发镜像
├── Dockerfile.backend             # 后端开发镜像
├── Dockerfile.prod                # 前端生产镜像
├── Dockerfile.static              # 前端静态导出镜像
├── k8s-deployment.yml             # K8s 生产部署
├── DEPLOYMENT.md                  # 部署指南
├── scripts/
│   ├── post-create.sh             # 创建后初始化
│   ├── post-start.sh              # 启动后脚本
│   └── build-and-serve.sh         # 一键构建并启动
├── .github/
│   └── workflows/
│       ├── backend-ci.yml         # 后端 CI/CD
│       ├── frontend-ci.yml        # 前端 CI/CD
│       ├── build-and-deploy.yml   # 合并部署
│       └── docker-build.yml       # 手动 Docker 构建
└── README.md                      # 本文档
```

## 快速开始（推荐：使用 Makefile）

项目根目录提供完整的 `Makefile`，支持开发/生产环境快速部署：

```bash
cd /home/wayne/tools/video_maker

# 查看所有可用命令
make help

# 启动开发环境（前端 + 后端 + Redis）
make dev

# 一键生产部署（构建 → 推送 → 启动）
make deploy

# 仅启动生产容器（使用本地镜像）
make prod-up
```

### 传统方式：脚本启动

```bash
cd /home/wayne/tools/video_maker/.devcontainer
./scripts/build-and-serve.sh
```

访问 http://localhost

### 2. 手动分步启动

**Step 1: 构建前端**
```bash
cd /home/wayne/tools/video_maker/frontend

# 创建静态导出配置
cat > next.config.js << 'EOF'
/** @type {import("next").NextConfig} */
const nextConfig = {
  output: "export",
  distDir: "dist",
  assetPrefix: ".",
  images: { unoptimized: true },
};
module.exports = nextConfig;
EOF

# 构建
npm run build
```

**Step 2: 启动 Nginx + 后端**
```bash
cd /home/wayne/tools/video_maker/.devcontainer
docker-compose -f docker-compose.simple.yml up --build -d
```

**访问:**
- 前端: http://localhost
- API: http://localhost/api/
- API Docs: http://localhost/api/docs

### 3. 停止服务

```bash
cd /home/wayne/tools/video_maker/.devcontainer
docker-compose -f docker-compose.simple.yml down
```

## 架构说明

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Nginx     │────▶│   Backend   │────▶│    Redis    │
│  (Port 80)  │     │  (Port 8000)│     │  (Port 6379)│
└──────┬──────┘     └─────────────┘     └─────────────┘
       │
       ▼  /api/* 转发
  静态文件服务
       │
       ▼
┌─────────────┐
│   Worker    │
└─────────────┘
```

## Nginx 配置

- `/` - 静态文件服务（前端 dist）
- `/api/*` - 转发到后端 (http://backend:8000)
- `/api/projects/*` - SSE 支持（无缓冲）

## CI/CD 工作流

| Workflow | 触发路径 | 任务 |
|----------|----------|------|
| `backend-ci.yml` | `backend/**` | uv sync → ruff → mypy → pytest → Docker build |
| `frontend-ci.yml` | `frontend/**` | npm ci → lint → tsc → build → Docker → GitHub Pages |

## 文件说明

| 文件 | 用途 |
|------|------|
| `docker-compose.yml` | **开发环境** - 包含前端 dev server、后端、Redis、Worker |
| `docker-compose.simple.yml` | **生产环境** - 简化版：Nginx + 后端 + Redis + Worker |
| `docker-compose.prod.yml` | **完整生产环境** - 包含前端 standalone + Nginx + 后端 + Redis + Worker |
| `docker-compose.override.yml` | 本地开发覆盖配置 |
| `podman-compose.yml` | Podman 容器编排（无 root 权限） |
| `k8s-deployment.yml` | Kubernetes 部署清单 |
| `nginx.conf` | Nginx 反向代理配置（API 转发 + 静态文件） |

## 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `NEXT_PUBLIC_API_BASE` | `/api` | 前端 API 基础路径（通过 nginx 代理） |
| `REDIS_URL` | `redis://redis:6379` | Redis 连接 |
| `DATABASE_URL` | `sqlite+aiosqlite:///...` | 数据库地址 |
| `CORS_ORIGINS` | - | 允许的跨域来源 |
