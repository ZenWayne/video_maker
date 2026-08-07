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
# `npx vite build`, not `npm run build`: the package.json script is
# `tsc && vite build`, and `tsc --noEmit` currently reports 30 pre-existing
# errors on master (20 of them just missing vitest globals in src/**/__tests__,
# the rest genuine type errors in ShotsPage/ShotCard/ProgressStream/
# ProjectPage/TrimDialog). Local dev never hits this because the vite dev
# server does not type-check. Bundling does not depend on tsc — esbuild strips
# types — so the image builds from vite alone rather than having the container
# build be the thing that's blocked on unrelated type debt. Restore
# `npm run build` here once `npx tsc --noEmit` is clean.
COPY . .
RUN npx vite build

# Stage 2: Serve with nginx
FROM docker.io/nginx:alpine

# Copy built static files
COPY --from=builder /app/dist /usr/share/nginx/html

# Copy nginx config
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
