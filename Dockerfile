# The pheasant image: every tier of a region (api, graph, indexer, worker,
# logger) and the web UI, from one build.
#
# 1. **Every optional code path is installed.** PHEASANT_EXTRAS defaults to all
#    of them, so any config a user writes actually works: semantic search
#    (lancedb), the agentic answer loop (langgraph), sandboxed connectors
#    (wasmtime), the gRPC worker transport, the analytics export and OTLP
#    traces. The state store's driver (pg8000 + SQLAlchemy's pool) and the
#    NATS client are core dependencies. Slim builds are one flag:
#      docker build --build-arg PHEASANT_EXTRAS=mcp .
#
# 2. **The UI is baked in and served by the api tier itself.** No sidecar, no
#    nginx, no second port and no CORS origin to get right.
#
# compose.yaml (pheasant-kb) runs this image once per tier, each with its own
# command and its own mounted config. With nothing mounted at /config the
# entrypoint writes a one-process config (standalone defaults); its state is
# still PostgreSQL, at PHEASANT_DATABASE_URL.

# --------------------------------------------------------------------------
# Stage 1 — the web UI bundle.
# --------------------------------------------------------------------------
FROM node:22-alpine AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
COPY extensions /extensions
ARG PHEASANT_BUILD_EXTENSIONS=
# Empty API base: the bundle is served by pheasant itself, so the API is
# same-origin at the root. (The sidecar image builds with "/api" instead,
# because there nginx proxies under that prefix.)
ENV VITE_PHEASANT_API_BASE=""
RUN PHEASANT_BUILD_EXTENSIONS="${PHEASANT_BUILD_EXTENSIONS}" npm run build

# --------------------------------------------------------------------------
# Stage 2 — the runtime.
# --------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="pheasant" \
      org.opencontainers.image.description="Docker-first MCP knowledge graph indexer and agentic retrieval layer" \
      org.opencontainers.image.source="https://github.com/esatt10/pheasant-kb" \
      org.opencontainers.image.licenses="AGPL-3.0-only OR LicenseRef-pheasant-commercial"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PHEASANT_CONFIG=/config/pheasant.yaml \
    PHEASANT_UI_DIST=/app/ui \
    PHEASANT_IN_CONTAINER=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

# Install from pyproject so the image's dependencies never drift from the
# package's declared deps (a hand-maintained list previously omitted core deps
# added later — numpy (21.4) and zstandard (21.6a) — breaking the smoke test).
ARG PHEASANT_EXTRAS=mcp,agent,vector,wasm,grpc,analytics,otel
COPY pyproject.toml README.md LICENSE /app/
# Resolve runtime dependencies before copying application source. A one-line
# code edit should not invalidate and redownload the entire universal stack;
# this layer now changes only when pyproject.toml or the selected extras do.
RUN PHEASANT_EXTRAS="${PHEASANT_EXTRAS}" python -c "import os,subprocess,sys,tomllib; p=tomllib.load(open('pyproject.toml','rb'))['project']; extras=[x for x in os.environ.get('PHEASANT_EXTRAS','').split(',') if x]; optional=p.get('optional-dependencies',{}); missing=[x for x in extras if x not in optional]; missing and (_ for _ in ()).throw(ValueError(f'unknown extras: {missing}')); deps=list(p['dependencies']) + [dep for x in extras for dep in optional[x]]; subprocess.check_call([sys.executable,'-m','pip','install','--no-cache-dir',*deps])"
COPY src /app/src
COPY scripts/benchmark_mutations.py /app/scripts/benchmark_mutations.py
COPY scripts/benchmark_job_publication.py /app/scripts/benchmark_job_publication.py
# Install only Pheasant's wheel/entry point; dependencies are the cached layer
# above and PYTHONPATH still points at the copied source tree.
RUN pip install --no-cache-dir --no-deps .
ARG PHEASANT_BUILD_EXTENSIONS=
RUN --mount=type=bind,source=extensions,target=/extensions \
    PHEASANT_BUILD_EXTENSIONS="${PHEASANT_BUILD_EXTENSIONS}" python -c "import os,re,subprocess,sys; names=[name.strip() for name in os.environ['PHEASANT_BUILD_EXTENSIONS'].split(',') if name.strip()]; assert all(re.fullmatch('[a-z][a-z0-9-]{0,39}',name) for name in names); names and subprocess.check_call([sys.executable,'-m','pip','install','--no-cache-dir','--no-deps',*['/extensions/'+name for name in names]])"

COPY --from=ui /ui/dist /app/ui
COPY pheasant.example.yaml /app/pheasant.default.yaml
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# Run as an unprivileged user. The indexer reads whatever paths it is pointed
# at, so running it as root means a misconfigured source — or a bug in the
# path handling — reads the container's entire filesystem, /proc/self/environ
# (which holds any API keys passed through `environment:`) included. The state
# and export volumes are chowned so the non-root user can still write them;
# source mounts stay read-only and only need read access.
#
# /config is writable by design: with no config bind-mounted, the entrypoint
# generates one there on first boot. A read-only bind mount still wins — the
# entrypoint only writes when the file is absent.
RUN useradd --create-home --uid 10001 pheasant \
    && mkdir -p /state /exports /workspace /config /memory \
    && chown -R pheasant:pheasant /state /exports /app /config /memory
USER pheasant
LABEL org.opencontainers.image.pheasant.authorization="roles-v1"

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8765/health || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["serve"]
