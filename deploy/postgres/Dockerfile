# pheasant's PostgreSQL: the official PostgreSQL 17 image plus pg_textsearch.
#
# Nothing else. pheasant needs PostgreSQL 17 with the pg_textsearch extension
# preloaded (storage.lexical_index: pg_textsearch, the BM25 arm); this image is
# exactly that, so it carries no TimescaleDB, no Timescale Toolkit and no other
# extension a deployment did not ask for. pg_prewarm and pg_stat_statements are
# PostgreSQL's own contrib modules and come with the base image.
#
# Pinned three ways, each recorded in deploy/postgres/README.md:
#   * the base by digest (the multi-arch index of postgres:17.11-bookworm),
#   * pg_textsearch by release tag and the sha256 of its source archive,
#   * the build's compiler and headers by being the base's own PGDG packages.
#
# Build:  docker build -t pheasant-postgres:17 deploy/postgres
# Arm64:  docker buildx build --platform linux/amd64,linux/arm64 deploy/postgres

ARG POSTGRES_BASE=postgres:17.11-bookworm@sha256:665dc7b37ac52a17e298511414c21d23ffa4e6a6ef6225b3e5d59c7afb35e994

FROM ${POSTGRES_BASE} AS build

ARG PG_TEXTSEARCH_VERSION=1.5.1
ARG PG_TEXTSEARCH_SHA256=2e7fb76ed96176afc6d16cc5d095ced2cb233ff27fbeaa8c7bfffc82e02f7498

# The server-dev package matching the base's exact server build, from the same
# PGDG repository the base installed the server from.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl \
        "postgresql-server-dev-${PG_MAJOR}=${PG_VERSION}"; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN set -eux; \
    curl -fsSL -o pg_textsearch.tar.gz \
        "https://github.com/timescale/pg_textsearch/archive/refs/tags/v${PG_TEXTSEARCH_VERSION}.tar.gz"; \
    echo "${PG_TEXTSEARCH_SHA256}  pg_textsearch.tar.gz" | sha256sum --check --strict; \
    tar -xzf pg_textsearch.tar.gz --strip-components=1; \
    make -j"$(nproc)"; \
    make install DESTDIR=/out; \
    install -D -m 0644 LICENSE /out/usr/share/doc/pg-textsearch/LICENSE; \
    install -D -m 0644 NOTICE /out/usr/share/doc/pg-textsearch/NOTICE

FROM ${POSTGRES_BASE}

ARG PG_TEXTSEARCH_VERSION=1.5.1

# Only the installed extension files cross over; the compiler does not.
COPY --from=build /out/ /

# Preload pg_textsearch in every cluster this image initializes. The sample is
# what initdb copies to postgresql.conf, so a new data directory starts with
# it, and a `-c shared_preload_libraries=...` on the command line still wins
# for a deployment that adds a library (pg_prewarm on Container Apps).
#
# The locale is the one every pheasant database has had (C.UTF-8, libc), so a
# restored dump sorts and compares text exactly as it did before.
RUN set -eux; \
    sample=/usr/share/postgresql/postgresql.conf.sample; \
    grep -q "^#shared_preload_libraries = ''" "$sample"; \
    sed -i "s/^#shared_preload_libraries = ''/shared_preload_libraries = 'pg_textsearch'/" "$sample"; \
    grep -q "^shared_preload_libraries = 'pg_textsearch'" "$sample"; \
    test -f "/usr/lib/postgresql/${PG_MAJOR}/lib/pg_textsearch.so"; \
    test -f "/usr/share/postgresql/${PG_MAJOR}/extension/pg_textsearch.control"

ENV POSTGRES_INITDB_ARGS="--locale-provider=libc --locale=C.UTF-8 --encoding=UTF8"

# The official entrypoint, behind one refusal: an empty data directory beside
# a previous pheasant database is an upgrade that has not moved its data yet,
# not a fresh install (pheasant-entrypoint.sh, README.md).
COPY --chmod=0755 pheasant-entrypoint.sh /usr/local/bin/pheasant-entrypoint.sh
ENTRYPOINT ["pheasant-entrypoint.sh"]
CMD ["postgres"]

LABEL org.opencontainers.image.title="pheasant-postgres" \
      org.opencontainers.image.description="PostgreSQL 17 with pg_textsearch preloaded, for pheasant" \
      org.opencontainers.image.source="https://github.com/esatt10/pheasant-kb" \
      org.opencontainers.image.licenses="PostgreSQL" \
      io.pheasant.pg_textsearch.version="${PG_TEXTSEARCH_VERSION}"
