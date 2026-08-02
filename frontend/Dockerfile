# CAFlow web — a static build served by nginx, which also fronts the API so
# the browser talks to a single origin (no CORS preflight on every call).

# --- build ------------------------------------------------------------------

FROM node:22-alpine AS builder

WORKDIR /build

# Dependencies are installed from the lockfile alone, so this layer is only
# rebuilt when the lockfile changes.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# --- runtime ----------------------------------------------------------------

FROM nginx:1.29-alpine AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /build/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget --spider -q http://127.0.0.1/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
