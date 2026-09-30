# --- Build stage: compile the static bundle ---
FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./

RUN npm ci --no-audit --no-fund

COPY index.html tsconfig.json vite.config.ts ./
COPY src ./src

# Same-origin /api paths in production; never bake a node IP into the bundle.
ENV VITE_API_BASE_URL=""

RUN npm run build

# --- Runtime stage: static files only, served by unprivileged nginx (UID 101, port 8080) ---
FROM nginxinc/nginx-unprivileged:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8080

USER 101
