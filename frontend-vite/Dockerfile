# syntax=docker/dockerfile:1

# Stage 1: Build
#
# node:24-alpine (npm 11.x), NOT node:20-alpine (npm 10.8.x): package-lock.json
# is a lockfileVersion-3 lock written by npm 11, and npm 10 computes a
# different ideal tree for the nested vite@8 that vitest@4 pulls in under a
# project pinned to vite@5 — so `npm ci` fails there with
# "Missing: esbuild@0.28.1 from lock file". Matching the npm major that
# generated the lock keeps `npm ci` reproducible without rewriting the
# dependency tree.
FROM docker.io/node:24-alpine AS builder
WORKDIR /app

# Install dependencies
COPY package.json package-lock.json* ./
RUN npm ci

# Copy source and build.
#
# `npm run build` (= `tsc && vite build`)，类型检查是构建的一部分。
# 这里曾经是 `npx vite build`，为的是绕开 master 上 30 个既有类型错误；
# 那些错误已全部修掉（`npx tsc --noEmit` 干净），门禁随之恢复。
# 注意本地开发不会暴露类型错误 —— vite dev server 不做类型检查，
# 所以这道闸是唯一能在合并前拦住类型退化的地方，不要再绕开它。
COPY . .
RUN npm run build

# Stage 2: Serve with nginx
FROM docker.io/nginx:alpine

# Copy built static files
COPY --from=builder /app/dist /usr/share/nginx/html

# Copy nginx config
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
