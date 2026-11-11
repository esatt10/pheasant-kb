# Build the static bundle, then serve it from nginx. This image is a SEPARATE
# workload from the pheasant indexing container; it only serves static files and
# proxies API calls, so the pheasant image and its lifecycle are unchanged.
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# Same-origin API base; nginx proxies /api/* to the pheasant container.
ARG VITE_PHEASANT_API_BASE=/api
ARG VITE_PHEASANT_GRAPH_NODE_LIMIT=1200
ARG VITE_PHEASANT_GRAPH_LINK_LIMIT=3600
ENV VITE_PHEASANT_API_BASE=$VITE_PHEASANT_API_BASE
ENV VITE_PHEASANT_GRAPH_NODE_LIMIT=$VITE_PHEASANT_GRAPH_NODE_LIMIT
ENV VITE_PHEASANT_GRAPH_LINK_LIMIT=$VITE_PHEASANT_GRAPH_LINK_LIMIT
RUN npm run build

FROM nginx:1.27-alpine AS runtime
LABEL org.opencontainers.image.title="pheasant UI" \
      org.opencontainers.image.description="Light React front end for pheasant" \
      org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0" \
      org.opencontainers.image.documentation="https://esatt10.github.io/pheasant-kb/licensing/"
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1/ >/dev/null 2>&1 || exit 1
