# ── 构建阶段 ──────────────────────────────────────────────────
FROM node:20-alpine AS builder

WORKDIR /app

# 先复制 lock 文件，利用 Docker 层缓存
COPY package*.json ./
RUN npm ci

COPY . .
RUN npm run build

# ── 静态托管阶段 ──────────────────────────────────────────────
FROM nginx:1.27-alpine

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
