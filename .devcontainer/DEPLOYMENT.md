# 构建和部署指南

本文档介绍如何构建和部署 Video Maker 前端应用。

## 文件说明

| 文件 | 用途 |
|------|------|
| `docker-compose.yml` | 本地 Docker Compose 部署 |
| `frontend/Dockerfile` | Docker 镜像构建 |
| `k8s-deployment.yml` | Kubernetes 部署配置 |
| `.github/workflows/build-and-deploy.yml` | GitHub Actions CI/CD |
| `.github/workflows/docker-build.yml` | 手动触发 Docker 构建 |

---

## 方式一：Docker Compose（推荐本地/服务器部署）

### 1. 构建并启动

```bash
cd /home/wayne/tools/video_maker

# 设置后端 API 地址
export NEXT_PUBLIC_API_BASE=http://localhost:8000

# 构建并启动
docker-compose up --build -d

# 查看日志
docker-compose logs -f frontend
```

### 2. 访问应用

打开浏览器访问 http://localhost:3000

### 3. 停止服务

```bash
docker-compose down
```

---

## 方式二：手动 Docker 构建

### 1. 构建镜像

```bash
cd /home/wayne/tools/video_maker/frontend

docker build -t video-maker-frontend:latest .
```

### 2. 运行容器

```bash
docker run -d \
  --name video-maker-frontend \
  -p 3000:3000 \
  -e NEXT_PUBLIC_API_BASE=http://localhost:8000 \
  --restart unless-stopped \
  video-maker-frontend:latest
```

---

## 方式三：Kubernetes 部署

### 1. 修改配置

编辑 `k8s-deployment.yml`：
- 替换 `USERNAME` 为你的 GitHub 用户名
- 替换 `video-maker.example.com` 为你的域名

### 2. 部署

```bash
kubectl apply -f k8s-deployment.yml
```

### 3. 查看状态

```bash
kubectl get pods -n video-maker
kubectl get svc -n video-maker
kubectl get ingress -n video-maker
```

---

## 方式四：GitHub Actions CI/CD

### 自动触发条件

- 推送到 `main` 或 `master` 分支时自动触发
- 修改 `frontend/**` 或 `.github/workflows/**` 路径下的文件时触发

### 需要配置的 Secrets

在 GitHub Repository Settings → Secrets and variables → Actions 中添加：

| Secret Name | 说明 | 必需 |
|-------------|------|------|
| `NEXT_PUBLIC_API_BASE` | 后端 API 地址 | 是 |
| `VERCEL_TOKEN` | Vercel 部署令牌 | 否（可选） |

### CI/CD 流程

1. **Build Job**: 安装依赖、运行 lint、构建应用
2. **Docker Job**: 构建并推送 Docker 镜像到 GHCR
3. **Deploy GitHub Pages Job**: 静态导出并部署到 GitHub Pages
4. **Deploy Vercel Job**: 部署到 Vercel（可选）

---

## 方式五：静态导出（用于任何静态托管）

### 1. 构建静态文件

```bash
cd /home/wayne/tools/video_maker/frontend

# 创建静态导出配置
echo 'module.exports = { output: "export", distDir: "dist", assetPrefix: "." };' > next.config.js

# 构建
npm ci
npm run build
```

### 2. 部署到任意静态托管

将 `frontend/dist` 目录上传到：
- GitHub Pages
- Netlify
- Vercel
- AWS S3 + CloudFront
- 阿里云 OSS
- 腾讯云 COS
- Nginx 服务器

---

## 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `NEXT_PUBLIC_API_BASE` | `http://localhost:8000` | 后端 API 基础地址 |
| `NODE_ENV` | `production` | 运行环境 |

---

## 故障排查

### 构建失败

```bash
# 清理缓存
docker-compose down -v
docker system prune -a

# 重新构建
docker-compose up --build -d
```

### 无法连接后端 API

1. 检查 `NEXT_PUBLIC_API_BASE` 是否正确
2. 确保后端服务已启动
3. 检查跨域配置（CORS）

### 端口冲突

修改 `docker-compose.yml` 中的端口映射：

```yaml
ports:
  - "8080:3000"  # 将主机 8080 端口映射到容器 3000 端口
```

---

## 生产环境建议

1. **使用 HTTPS**: 配置 SSL 证书
2. **CDN 加速**: 静态资源使用 CDN
3. **监控告警**: 配置健康检查和告警
4. **日志收集**: 集中收集和分析日志
5. **自动扩缩容**: K8s 环境配置 HPA
