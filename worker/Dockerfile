# Audit 3.3: hardened worker image.
#   - Non-root USER (1000:1000)
#   - `docker.io` package removed — worker talks to docker-socket-proxy over
#     tcp:2375, not to the host docker CLI.
#   - COPY . . moved after `pip install` so a code-only change doesn't
#     invalidate the dependency layer.
#   - Minimal HEALTHCHECK so the compose file and k8s readiness probes have a
#     meaningful signal even if main.py is stuck.
FROM python:3.11-slim-bookworm

# Install only the packages the worker actually needs. No docker.io — the
# socket proxy handles all Docker API calls over TCP.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        libpq-dev \
        gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first for cache reuse.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Code second — avoids invalidating the deps layer on every edit.
COPY . .

# Create an unprivileged user and chown the app directory so the worker can
# write to /app/sandbox and its caches.
RUN groupadd --system --gid 1000 zovark \
    && useradd --system --uid 1000 --gid 1000 --home-dir /app --shell /usr/sbin/nologin zovark \
    && chown -R zovark:zovark /app

USER zovark:zovark

# Lightweight liveness — verifies the Python import graph is intact. Runtime
# dependency checks (DB, redis, temporal) live in scripts/stack_healthcheck.sh.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import temporalio, psycopg2, redis" || exit 1

CMD ["python", "main.py"]
