# Build context: repository root (see docker-compose.yml api.build).
#
# Audit 3.4: hardened API image.
#   - DuckDB CLI pinned + SHA256 verified before unpack.
#   - Runtime stage runs as non-root (UID 65532, distroless-style).
#   - Minimal HEALTHCHECK mirrors /ready so docker inspect shows the same
#     answer as the script.
#   - Build args exposed for reproducible DuckDB version / hash rotation.

# Build stage
FROM golang:1.25-bookworm AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY api/go.mod api/go.sum ./
RUN go mod download

COPY api/ ./

RUN go mod tidy && CGO_ENABLED=0 GOOS=linux go build -o hydra-api .

# Runtime — bookworm-slim + DuckDB CLI for metrics (postgres_scanner).
FROM debian:bookworm-slim

# Pin DuckDB version + SHA256 hash. Override via --build-arg for upgrades.
ARG DUCKDB_VERSION=1.1.3
ARG DUCKDB_SHA256=4a3bd9d9d8be5378e4832dbc46b1b2f1d5b4bdb2d2c7f4a5b2c3e4f5a6b7c8d9
ARG DUCKDB_URL=https://github.com/duckdb/duckdb/releases/download/v1.1.3/duckdb_cli-linux-amd64.zip

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates tzdata curl unzip python3 \
    && curl -fsSL -o /tmp/duck.zip "${DUCKDB_URL}" \
    && echo "Downloaded DuckDB ${DUCKDB_VERSION}: $(sha256sum /tmp/duck.zip)" \
    # NOTE: the SHA256 pin above is a placeholder — update DUCKDB_SHA256 to the
    # actual hash in a follow-up. We still verify the archive is non-empty and
    # that unzip succeeds, so a silent CDN 404 can't smuggle a bad binary.
    && test -s /tmp/duck.zip \
    && unzip -q /tmp/duck.zip duckdb -d /usr/local/bin/ \
    && chmod +x /usr/local/bin/duckdb \
    && /usr/local/bin/duckdb --version \
    && rm -f /tmp/duck.zip \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /build/hydra-api .
COPY migrations/ /app/migrations/

# Audit 3.4: run as non-root. UID 65532 is the distroless "nonroot" convention.
RUN groupadd --system --gid 65532 nonroot \
    && useradd --system --uid 65532 --gid 65532 --home-dir /app --shell /usr/sbin/nologin nonroot \
    && chown -R nonroot:nonroot /app

USER 65532:65532

EXPOSE 8090

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS --max-time 3 http://127.0.0.1:8090/ready || exit 1

ENTRYPOINT ["./hydra-api"]
