#!/usr/bin/env python3
"""
ZOVARK FLEET AGENT — Self-Healing Daemon with Embedded Sneakernet UI
=====================================================================
Autonomous watchdog that monitors all Zovark services, performs health
checks, auto-restarts failed containers, diagnoses crashes via local LLM,
and serves a SOC War Room management console on port 8081.

Runs as a Docker container with access to the Docker socket.

Features:
  - Service discovery via docker ps
  - HTTP/TCP/CLI health checks per service type
  - 3-level restart escalation (restart -> restart+deps -> critical stop)
  - AI crash diagnosis via local LLM inference (Llama 3.2 3B)
  - Embedded HTML management UI (no external deps, air-gap safe)
  - Worker stuck detection (0 completions + pending > 0 for 10 min)
  - Disk pressure monitoring (warn 90%, auto-prune 95%)
  - Daily JSON reports at /var/log/zovark/
  - Status API on port 8081

Author: Zovark Fleet Agent v1.0
"""

import asyncio
import json
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
import shutil
import subprocess
import threading
import time
import urllib.request
import urllib.error
from collections import deque
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

# ── Configuration ──────────────────────────────────────────────────────
# Aligned with worker/settings.py ZOVARK_ env prefix.
# Healer runs in its own container (no pydantic), so reads env vars directly.

CHECK_INTERVAL = int(os.environ.get("HEALER_CHECK_INTERVAL", "30"))
REDIS_PASSWORD = os.environ.get("ZOVARK_REDIS_PASSWORD", os.environ.get("REDIS_PASSWORD", "hydra-redis-dev-2026"))
POSTGRES_USER = os.environ.get("ZOVARK_DB_USER", os.environ.get("POSTGRES_USER", "zovark"))
POSTGRES_PASSWORD = os.environ.get("ZOVARK_DB_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "hydra_dev_2026"))
POSTGRES_DB = os.environ.get("ZOVARK_DB_NAME", os.environ.get("POSTGRES_DB", "zovark"))
LLM_MODEL = os.environ.get("ZOVARK_LLM_FAST_MODEL", os.environ.get("ZOVARK_MODEL_FAST", "nemotron-mini-4b"))
LLM_HOST = os.environ.get("ZOVARK_LLM_BASE_URL", "http://zovark-inference:8080")
LLM_PROVIDER = os.environ.get("ZOVARK_LLM_PROVIDER", "local").lower().strip()
LLM_API_KEY = (
    os.environ.get("OPENAI_API_KEY", "").strip()
    or os.environ.get("ZOVARK_OPENAI_API_KEY", "").strip()
    or os.environ.get("ZOVARK_LLM_KEY", "").strip()
)
OTEL_ENABLED = os.environ.get("ZOVARK_OTEL_ENABLED", os.environ.get("OTEL_ENABLED", "false")).lower() in ("true", "1", "yes")
LOG_DIR = Path("/var/log/zovark")
API_PORT = 8081
MAX_EVENTS = 500
MAX_DIAGNOSES = 100
WORKER_STUCK_THRESHOLD = 600  # 10 minutes in seconds
DISK_WARN_PCT = 90
DISK_CRITICAL_PCT = 95
MAX_RESTART_ATTEMPTS = 3
SYNTHETIC_LOGIN_INTERVAL = 60  # seconds between synthetic login checks
SYNTHETIC_LOGIN_URL = os.environ.get(
    "SYNTHETIC_LOGIN_URL", "http://zovark-dashboard:3000/api/v1/auth/login"
)
SYNTHETIC_LOGIN_EMAIL = os.environ.get("SYNTHETIC_LOGIN_EMAIL", "admin@test.local")
SYNTHETIC_LOGIN_PASSWORD = os.environ.get("SYNTHETIC_LOGIN_PASSWORD", "TestPass2026")

# ── Logging ────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [HEALER] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("healer")

# ── OTLP Log Export (optional — degrades gracefully) ──────────────────
_otel_log_provider = None

def _init_otel_logging():
    """Export healer logs to SigNoz via OTLP HTTP when OTEL_ENABLED."""
    global _otel_log_provider
    if not OTEL_ENABLED:
        return
    otel_endpoint = os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        os.environ.get("ZOVARK_OTEL_ENDPOINT", "http://zovark-signoz-collector:4318"),
    ).rstrip("/")
    try:
        import atexit
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create({
            "service.name": "zovark-healer",
            "service.version": "1.1",
            "deployment.environment": os.environ.get("ZOVARK_ENV", "development"),
        })
        provider = LoggerProvider(resource=resource)
        provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=f"{otel_endpoint}/v1/logs"),
                max_export_batch_size=32,
                schedule_delay_millis=2000,
                export_timeout_millis=10_000,
            )
        )
        set_logger_provider(provider)
        _otel_log_provider = provider

        handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)
        logging.getLogger().addHandler(handler)

        def _flush():
            try:
                provider.shutdown()
            except Exception:
                pass
        atexit.register(_flush)
        log.info("OTLP log export enabled → %s/v1/logs", otel_endpoint)
    except ImportError:
        log.info("opentelemetry log SDK not installed, OTLP log export skipped")
    except Exception as e:
        log.warning("OTLP log export init failed (non-fatal): %s", e)

_init_otel_logging()

# ── Global State ───────────────────────────────────────────────────────

services: dict = {}           # name -> ServiceState
events: deque = deque(maxlen=MAX_EVENTS)
diagnoses: deque = deque(maxlen=MAX_DIAGNOSES)
healer_start_time: float = time.time()
lock = threading.Lock()
worker_zero_completions_since: dict = {}  # container_id -> timestamp


class ServiceState:
    """Tracks health and restart state for a single service."""

    def __init__(self, name: str, container_id: str, container_name: str, svc_type: str):
        self.name = name
        self.container_id = container_id
        self.container_name = container_name
        self.svc_type = svc_type  # api, worker, postgres, redis, dashboard, temporal, inference, other
        self.status = "unknown"   # healthy, degraded, down, unknown
        self.last_check = None
        self.last_healthy = None
        self.restart_count = 0
        self.escalation_level = 0  # 0=none, 1=restart, 2=restart+deps, 3=critical
        self.consecutive_failures = 0
        self.last_error = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "container_id": self.container_id[:12],
            "container_name": self.container_name,
            "type": self.svc_type,
            "status": self.status,
            "last_check": self.last_check,
            "last_healthy": self.last_healthy,
            "restart_count": self.restart_count,
            "escalation_level": self.escalation_level,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
        }


def emit_event(level: str, service: str, message: str, detail: str = ""):
    """Record an audit event."""
    evt = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "service": service,
        "message": message,
        "detail": detail[:500],
    }
    with lock:
        events.append(evt)
    log.log(
        {"INFO": logging.INFO, "WARN": logging.WARNING, "ERROR": logging.ERROR,
         "CRITICAL": logging.CRITICAL}.get(level, logging.INFO),
        "[%s] %s — %s", service, message, detail[:200],
    )


# ── Service Discovery ─────────────────────────────────────────────────

SERVICE_TYPE_MAP = {
    "zovark-api": "api",
    "zovark-postgres": "postgres",
    "zovark-redis": "redis",
    "zovark-dashboard": "dashboard",
    "zovark-temporal": "temporal",
    "zovark-pgbouncer": "pgbouncer",
    "zovark-egress-proxy": "squid",
    "zovark-healer": "self",
    # SigNoz tracing stack (docker compose includes zovark-clickhouse + collector + unified signoz)
    "hydra-mvp-zovark-clickhouse-1": "signoz_clickhouse",
    "hydra-mvp-zovark-signoz-collector-1": "signoz_collector",
    "zovark-signoz": "signoz_app",
    # Legacy split-stack container names (pre–v0.76 unified binary)
    "hydra-mvp-zovark-signoz-query-1": "signoz_query",
    "hydra-mvp-zovark-signoz-frontend-1": "signoz_frontend",
}

WORKER_PATTERN = re.compile(r"zovark[-_]worker[-_]\d+")


def classify_container(container_name: str) -> str:
    """Determine service type from container name."""
    if container_name in SERVICE_TYPE_MAP:
        return SERVICE_TYPE_MAP[container_name]
    if WORKER_PATTERN.match(container_name):
        return "worker"
    if "worker" in container_name.lower():
        return "worker"
    return "other"


def init_services():
    """Discover running containers and build service registry."""
    global services
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.ID}}|{{.Names}}|{{.Status}}"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            log.error("docker ps failed: %s", result.stderr)
            return

        new_services = {}
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.strip().split("|")
            if len(parts) < 3:
                continue
            cid, cname, _ = parts[0], parts[1], parts[2]
            svc_type = classify_container(cname)
            if svc_type == "self":
                continue  # don't monitor ourselves
            svc = ServiceState(
                name=cname,
                container_id=cid,
                container_name=cname,
                svc_type=svc_type,
            )
            # Carry forward state from previous cycle
            if cname in services:
                old = services[cname]
                svc.restart_count = old.restart_count
                svc.escalation_level = old.escalation_level
                svc.last_healthy = old.last_healthy
                svc.consecutive_failures = old.consecutive_failures
            new_services[cname] = svc

        # Inference health is checked via LLM_HOST connectivity check, not as a managed container.
        # The zovark-inference container is discovered via docker ps above.

        with lock:
            services = new_services

        emit_event("INFO", "healer", f"Service discovery complete: {len(new_services)} services",
                    ", ".join(sorted(new_services.keys())))

    except Exception as e:
        log.error("Service discovery error: %s", e)


# ── Health Check Implementations ───────────────────────────────────────

def check_http(url: str, timeout: int = 5) -> tuple[bool, str]:
    """HTTP GET health check. Uses async httpx if available, falls back to urllib."""
    if HAS_HTTPX:
        return _check_http_sync_via_httpx(url, timeout)
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status < 400:
                return True, f"HTTP {resp.status}"
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, str(e)[:200]


def _check_http_sync_via_httpx(url: str, timeout: int = 5) -> tuple[bool, str]:
    """Non-blocking HTTP check using httpx (no GIL contention with subprocess)."""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url)
            if resp.status_code < 400:
                return True, f"HTTP {resp.status_code}"
            return False, f"HTTP {resp.status_code}"
    except httpx.ConnectError as e:
        return False, f"Connection refused: {str(e)[:150]}"
    except httpx.TimeoutException:
        return False, f"Timeout after {timeout}s"
    except Exception as e:
        return False, str(e)[:200]


async def check_http_async(url: str, timeout: int = 5) -> tuple[bool, str]:
    """Fully async HTTP health check."""
    if not HAS_HTTPX:
        return await asyncio.to_thread(check_http, url, timeout)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            if resp.status_code < 400:
                return True, f"HTTP {resp.status_code}"
            return False, f"HTTP {resp.status_code}"
    except httpx.ConnectError as e:
        return False, f"Connection refused: {str(e)[:150]}"
    except httpx.TimeoutException:
        return False, f"Timeout after {timeout}s"
    except Exception as e:
        return False, str(e)[:200]


def check_postgres(container_name: str) -> tuple[bool, str]:
    """PostgreSQL health check via docker exec + psql."""
    try:
        result = subprocess.run(
            ["docker", "exec", container_name,
             "pg_isready", "-U", POSTGRES_USER, "-d", POSTGRES_DB],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return True, "pg_isready OK"
        return False, result.stderr.strip() or result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "pg_isready timeout"
    except Exception as e:
        return False, str(e)[:200]


def check_redis(container_name: str) -> tuple[bool, str]:
    """Valkey health check via docker exec + valkey-cli."""
    try:
        result = subprocess.run(
            ["docker", "exec", container_name,
             "valkey-cli", "-a", REDIS_PASSWORD, "ping"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and "PONG" in result.stdout:
            return True, "PONG"
        return False, result.stderr.strip() or result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "valkey-cli timeout"
    except Exception as e:
        return False, str(e)[:200]


def check_temporal(container_name: str) -> tuple[bool, str]:
    """Temporal health check via gRPC port probe."""
    try:
        result = subprocess.run(
            ["docker", "exec", container_name,
             "tctl", "--ad", "localhost:7233", "cluster", "health"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return True, "temporal healthy"
        # Fallback: check if process is running
        result2 = subprocess.run(
            ["docker", "exec", container_name, "ls", "/"],
            capture_output=True, text=True, timeout=5,
        )
        if result2.returncode == 0:
            return True, "temporal container responsive"
        return False, result.stderr.strip()[:200]
    except subprocess.TimeoutExpired:
        return False, "tctl timeout"
    except Exception as e:
        return False, str(e)[:200]


def check_container_running(container_name: str) -> tuple[bool, str]:
    """Generic check: is the container running?"""
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and "true" in result.stdout.lower():
            return True, "container running"
        return False, "container not running"
    except Exception as e:
        return False, str(e)[:200]


def check_openai_api() -> tuple[bool, str]:
    """OpenAI API reachability via GET /v1/models (requires ZOVARK_LLM_KEY)."""
    try:
        base = LLM_HOST.rstrip("/")
        url = f"{base}/v1/models"
        req = urllib.request.Request(url, method="GET")
        if LLM_API_KEY:
            req.add_header("Authorization", f"Bearer {LLM_API_KEY}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return True, "OpenAI /v1/models OK"
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "OpenAI returned 401 — check ZOVARK_LLM_KEY"
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)[:200]


def check_tcp(host: str, port: int, timeout: int = 5) -> tuple[bool, str]:
    """TCP connect health check (e.g. ClickHouse native port)."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"TCP {host}:{port} open"
    except socket.timeout:
        return False, f"TCP {host}:{port} timeout after {timeout}s"
    except OSError as e:
        return False, f"TCP {host}:{port} refused: {e}"


def health_check(svc: ServiceState) -> tuple[bool, str]:
    """Route health check to the appropriate implementation."""
    try:
        if svc.svc_type == "api":
            return check_http("http://zovark-api:8090/health")
        elif svc.svc_type == "dashboard":
            return check_http("http://zovark-dashboard:3000/")
        elif svc.svc_type == "postgres":
            return check_postgres(svc.container_name)
        elif svc.svc_type == "redis":
            return check_redis(svc.container_name)
        elif svc.svc_type == "temporal":
            return check_container_running(svc.container_name)
        elif svc.svc_type == "inference":
            if LLM_PROVIDER == "openai":
                return check_openai_api()
            return check_http(f"{LLM_HOST}/api/tags")
        elif svc.svc_type == "worker":
            return check_container_running(svc.container_name)
        elif svc.svc_type == "pgbouncer":
            return check_container_running(svc.container_name)
        # Signoz tracing stack (only meaningful when OTEL is enabled)
        elif svc.svc_type == "signoz_collector":
            if not OTEL_ENABLED:
                return True, "OTEL disabled, skipping"
            # OTLP HTTP receiver — TCP check on port 4318 (GET returns 405 which is fine)
            return check_tcp("zovark-signoz-collector", 4318)
        elif svc.svc_type == "signoz_clickhouse":
            if not OTEL_ENABLED:
                return True, "OTEL disabled, skipping"
            return check_tcp("zovark-clickhouse", 9000)
        elif svc.svc_type == "signoz_app":
            if not OTEL_ENABLED:
                return True, "OTEL disabled, skipping"
            return check_http("http://zovark-signoz:8080/api/v1/health")
        elif svc.svc_type == "signoz_query":
            if not OTEL_ENABLED:
                return True, "OTEL disabled, skipping"
            return check_http("http://zovark-signoz-query:8080/api/v1/health")
        elif svc.svc_type == "signoz_frontend":
            if not OTEL_ENABLED:
                return True, "OTEL disabled, skipping"
            return check_http("http://zovark-signoz-frontend:3301/")
        else:
            return check_container_running(svc.container_name)
    except Exception as e:
        return False, f"check error: {e}"


# ── Worker Stuck Detection ─────────────────────────────────────────────

def check_worker_stuck(svc: ServiceState):
    """Detect workers that are running but not processing tasks."""
    if svc.svc_type != "worker":
        return
    try:
        # Check temporal workflow count
        result = subprocess.run(
            ["docker", "exec", "zovark-temporal",
             "tctl", "--ad", "localhost:7233", "--ns", "default",
             "workflow", "list", "--open", "--ps", "1"],
            capture_output=True, text=True, timeout=15,
        )
        has_pending = bool(result.stdout.strip()) and result.returncode == 0

        if has_pending:
            cid = svc.container_id
            now = time.time()
            if cid not in worker_zero_completions_since:
                worker_zero_completions_since[cid] = now
            elif now - worker_zero_completions_since[cid] > WORKER_STUCK_THRESHOLD:
                emit_event("WARN", svc.name,
                           "Worker appears stuck — pending workflows but no completions for 10+ minutes",
                           "Triggering restart")
                restart_container(svc, reason="worker stuck detection")
                worker_zero_completions_since.pop(cid, None)
        else:
            worker_zero_completions_since.pop(svc.container_id, None)

    except Exception as e:
        log.debug("Worker stuck check failed for %s: %s", svc.name, e)


# ── Auto-Restart Escalation ───────────────────────────────────────────

DEPENDENCY_MAP = {
    "api": ["postgres", "pgbouncer", "temporal", "redis"],
    "worker": ["postgres", "temporal", "redis"],
    "pgbouncer": ["postgres"],
    "dashboard": ["api"],
}


def get_container_logs(container_name: str, lines: int = 50) -> str:
    """Fetch the last N log lines from a container."""
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", str(lines), container_name],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stdout + result.stderr
        return output[-3000:] if len(output) > 3000 else output
    except Exception:
        return "(unable to fetch logs)"


def restart_container(svc: ServiceState, reason: str = "health check failure"):
    """Restart a container with 3-level escalation."""
    svc.restart_count += 1
    svc.escalation_level = min(svc.consecutive_failures, 3)

    if svc.escalation_level <= 1:
        # Level 1: Simple restart
        emit_event("WARN", svc.name, f"RESTART (level 1) — {reason}")
        try:
            subprocess.run(
                ["docker", "restart", svc.container_name],
                capture_output=True, text=True, timeout=60,
            )
        except Exception as e:
            emit_event("ERROR", svc.name, f"Restart failed: {e}")

    elif svc.escalation_level == 2:
        # Level 2: Restart container + its dependencies
        emit_event("WARN", svc.name, f"RESTART (level 2 — with dependencies) — {reason}")
        deps = DEPENDENCY_MAP.get(svc.svc_type, [])
        containers_to_restart = [svc.container_name]
        with lock:
            for dep_type in deps:
                for s in services.values():
                    if s.svc_type == dep_type and s.status != "healthy":
                        containers_to_restart.append(s.container_name)

        for cname in containers_to_restart:
            try:
                subprocess.run(
                    ["docker", "restart", cname],
                    capture_output=True, text=True, timeout=60,
                )
                emit_event("INFO", svc.name, f"Restarted dependency: {cname}")
            except Exception as e:
                emit_event("ERROR", svc.name, f"Dependency restart failed ({cname}): {e}")

    else:
        # Level 3: Critical — stop restarting, alert only
        emit_event("CRITICAL", svc.name,
                    f"CRITICAL — {reason} — exceeded max restarts, manual intervention required",
                    f"Restart count: {svc.restart_count}, consecutive failures: {svc.consecutive_failures}")

    # Trigger AI diagnosis in background
    if svc.svc_type not in ("inference", "self"):
        threading.Thread(
            target=ai_diagnose,
            args=(svc.name, svc.container_name, reason),
            daemon=True,
        ).start()


# ── AI Crash Diagnosis ─────────────────────────────────────────────────

def _rule_based_crash_diagnosis(logs: str, reason: str) -> str:
    blob = f"{reason}\n{logs}".lower()
    if "out of memory" in blob or "oom" in blob or "cannot allocate memory" in blob:
        return (
            "Rule-based diagnosis: memory exhaustion likely. Increase container memory "
            "or reduce concurrent workloads. Severity: high."
        )
    if "connection refused" in blob or "econnrefused" in blob:
        return (
            "Rule-based diagnosis: dependency unreachable (connection refused). "
            "Verify upstream service health and network/DNS. Severity: medium."
        )
    if "permission denied" in blob or "eacces" in blob:
        return (
            "Rule-based diagnosis: filesystem permission denied. Check volume mounts "
            "and UID/GID. Severity: medium."
        )
    if "exit code 1" in blob or "exited with code 1" in blob:
        return (
            "Rule-based diagnosis: generic non-zero exit. Inspect full logs for stack "
            "trace. Severity: medium."
        )
    return (
        "Rule-based diagnosis: no specific pattern matched. Collect full logs and "
        "check recent config changes. Severity: low."
    )


def ai_diagnose(service_name: str, container_name: str, reason: str):
    """Feed last 50 log lines to local LLM for crash diagnosis (10s cap), else rule-based."""
    try:
        logs = get_container_logs(container_name, lines=50)
        if not logs or logs.strip() == "(unable to fetch logs)":
            return

        prompt = (
            f"You are a DevOps expert. The container '{service_name}' has failed.\n"
            f"Failure reason: {reason}\n\n"
            f"Last 50 log lines:\n```\n{logs}\n```\n\n"
            "Provide a brief diagnosis (3-5 sentences):\n"
            "1. Root cause\n"
            "2. Recommended fix\n"
            "3. Severity (low/medium/high/critical)\n"
        )

        def _llm_call() -> str:
            if LLM_PROVIDER == "openai":
                url = f"{LLM_HOST.rstrip('/')}/v1/chat/completions"
                payload = json.dumps({
                    "model": LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 300,
                    "temperature": 0.3,
                }).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {LLM_API_KEY}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                choices = body.get("choices") or []
                if not choices:
                    return "(no response)"
                msg = choices[0].get("message") or {}
                return (msg.get("content") or "(no response)").strip()

            payload = json.dumps({
                "model": LLM_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.3, "num_predict": 300},
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{LLM_HOST}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body.get("response", "(no response)")

        diagnosis_text = None
        used_fallback = False
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                diagnosis_text = pool.submit(_llm_call).result(timeout=10)
        except FuturesTimeout:
            diagnosis_text = _rule_based_crash_diagnosis(logs, reason)
            used_fallback = True
            diagnosis_text += " [LLM diagnosis timed out after 10s]"
        except Exception as e:
            log.warning("AI diagnosis LLM call failed for %s: %s", service_name, e)
            diagnosis_text = _rule_based_crash_diagnosis(logs, reason)
            used_fallback = True
            diagnosis_text += f" [LLM unavailable: {e}]"

        diagnosis = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": service_name,
            "reason": reason,
            "diagnosis": diagnosis_text.strip(),
            "model": LLM_MODEL if not used_fallback else f"{LLM_MODEL}+rules",
            "log_snippet": logs[-500:],
        }

        with lock:
            diagnoses.append(diagnosis)

        emit_event("INFO", service_name,
                    f"AI diagnosis complete",
                    diagnosis_text[:300])

    except Exception as e:
        log.warning("AI diagnosis failed for %s: %s", service_name, e)
        diagnosis = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": service_name,
            "reason": reason,
            "diagnosis": f"(diagnosis error: {e})",
            "model": LLM_MODEL,
            "log_snippet": "",
        }
        with lock:
            diagnoses.append(diagnosis)


# ── Synthetic Transaction Health Check ─────────────────────────────────

def check_synthetic_login() -> dict:
    """
    End-to-end synthetic login through the dashboard's nginx proxy.
    Catches stale DNS cache after API container recreation (502 errors).
    Returns {"ok": bool, "status_code": int, "detail": str, "auto_fixed": bool}.
    """
    result = {"ok": False, "status_code": 0, "detail": "", "auto_fixed": False}
    payload = json.dumps({
        "email": SYNTHETIC_LOGIN_EMAIL,
        "password": SYNTHETIC_LOGIN_PASSWORD,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            SYNTHETIC_LOGIN_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            result["status_code"] = resp.status
            if resp.status < 400 and "token" in body:
                result["ok"] = True
                result["detail"] = "Login OK"
            else:
                result["detail"] = f"HTTP {resp.status} but no token in response"
    except urllib.error.HTTPError as e:
        result["status_code"] = e.code
        result["detail"] = f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        result["detail"] = f"Connection failed: {e.reason}"
    except Exception as e:
        result["detail"] = f"{type(e).__name__}: {str(e)[:200]}"

    # Auto-fix: if 502 or connection refused, restart dashboard for DNS refresh
    if not result["ok"] and result["status_code"] in (502, 503, 0):
        emit_event(
            "WARN", "synthetic_login",
            f"Synthetic login failed (HTTP {result['status_code']}), "
            f"restarting dashboard for DNS refresh",
            result["detail"],
        )
        try:
            subprocess.run(
                ["docker", "restart", "zovark-dashboard"],
                capture_output=True, text=True, timeout=30,
            )
            result["auto_fixed"] = True
            emit_event("INFO", "synthetic_login",
                       "Dashboard restarted for DNS refresh")
        except Exception as e:
            emit_event("ERROR", "synthetic_login",
                       f"Dashboard restart failed: {e}")
    elif not result["ok"]:
        # Non-proxy issue (e.g., 401 bad credentials, 500 API error)
        emit_event("WARN", "synthetic_login",
                   f"Synthetic login failed: {result['detail']}")

    return result


# ── Connectivity Health Checks ─────────────────────────────────────────

CONNECTIVITY_CHECK_INTERVAL = 60  # seconds

def check_connectivity() -> dict:
    """
    End-to-end connectivity checks across service boundaries.
    Tests that services can actually reach their dependencies, not just that
    processes are running.
    """
    results = {}

    # 1. API → DB+Redis+Temporal (via /ready endpoint)
    api_ready = {"ok": False, "detail": ""}
    try:
        req = urllib.request.Request("http://zovark-api:8090/ready", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if resp.status == 200:
                api_ready = {"ok": True, "detail": "all dependencies healthy"}
            else:
                # 503 — find which dependency is down
                checks = body.get("checks", {})
                failed = [k for k, v in checks.items() if not v.get("ready")]
                api_ready = {"ok": False, "detail": f"dependencies down: {', '.join(failed)}"}
    except urllib.error.HTTPError as e:
        if e.code == 503:
            try:
                body = json.loads(e.read().decode("utf-8"))
                checks = body.get("checks", {})
                failed = [k for k, v in checks.items() if not v.get("ready")]
                api_ready = {"ok": False, "detail": f"dependencies down: {', '.join(failed)}"}
            except Exception:
                api_ready = {"ok": False, "detail": f"HTTP {e.code}"}
        else:
            api_ready = {"ok": False, "detail": f"HTTP {e.code}"}
    except Exception as e:
        api_ready = {"ok": False, "detail": str(e)[:200]}

    results["api_readiness"] = api_ready
    if not api_ready["ok"]:
        emit_event("WARN", "connectivity", f"API not ready: {api_ready['detail']}")
        # If API can't reach DB, restarting API might help (reconnect pool)
        if "postgresql" in api_ready.get("detail", ""):
            emit_event("WARN", "connectivity",
                       "API lost DB connection, restarting API")
            try:
                subprocess.run(["docker", "restart", "zovark-api"],
                               capture_output=True, text=True, timeout=30)
            except Exception as e:
                emit_event("ERROR", "connectivity", f"API restart failed: {e}")

    # 2. Worker → LLM inference (availability check)
    llm_ok = {"ok": False, "detail": ""}
    try:
        if LLM_PROVIDER == "openai":
            ok, detail = check_openai_api()
            llm_ok = {"ok": ok, "detail": detail}
        else:
            req = urllib.request.Request(f"{LLM_HOST}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    body = json.loads(resp.read().decode("utf-8"))
                    models = [m.get("name", "?") for m in body.get("models", [])]
                    llm_ok = {"ok": True, "detail": f"models: {', '.join(models[:3])}"}
                else:
                    llm_ok = {"ok": False, "detail": f"HTTP {resp.status}"}
    except Exception as e:
        llm_ok = {"ok": False, "detail": str(e)[:200]}

    results["worker_to_llm"] = llm_ok
    if not llm_ok["ok"]:
        emit_event("WARN", "connectivity",
                   f"LLM inference unreachable from healer: {llm_ok['detail']}")

    # 3. Dashboard → API (synthetic login — already handled by check_synthetic_login)
    # Skip here to avoid duplicate restarts; check_synthetic_login runs on its own timer.

    return results


# ── Disk Pressure Monitoring ──────────────────────────────────────────

def check_disk_pressure():
    """Monitor disk usage, warn at 90%, auto-prune at 95%."""
    try:
        usage = shutil.disk_usage("/")
        pct = (usage.used / usage.total) * 100

        if pct >= DISK_CRITICAL_PCT:
            emit_event("CRITICAL", "disk",
                       f"Disk usage CRITICAL: {pct:.1f}% — auto-pruning Docker resources",
                       f"Used: {usage.used // (1024**3)}GB / {usage.total // (1024**3)}GB")
            # Auto-prune stopped containers and dangling images
            subprocess.run(
                ["docker", "system", "prune", "-f", "--volumes",
                 "--filter", "until=72h"],
                capture_output=True, text=True, timeout=120,
            )
            emit_event("INFO", "disk", "Docker system prune completed")

        elif pct >= DISK_WARN_PCT:
            emit_event("WARN", "disk",
                       f"Disk usage WARNING: {pct:.1f}%",
                       f"Used: {usage.used // (1024**3)}GB / {usage.total // (1024**3)}GB")

    except Exception as e:
        log.warning("Disk pressure check failed: %s", e)


# ── Daily Report ──────────────────────────────────────────────────────

def generate_daily_report():
    """Write a JSON report to /var/log/zovark/."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        report = {
            "generated_at": now.isoformat(),
            "uptime_seconds": int(time.time() - healer_start_time),
            "services": {},
            "total_events": len(events),
            "total_diagnoses": len(diagnoses),
            "summary": {
                "healthy": 0,
                "degraded": 0,
                "down": 0,
                "unknown": 0,
            },
        }

        with lock:
            for name, svc in services.items():
                report["services"][name] = svc.to_dict()
                bucket = svc.status if svc.status in report["summary"] else "unknown"
                report["summary"][bucket] += 1

            # Recent events (last 50)
            report["recent_events"] = list(events)[-50:]
            # Recent diagnoses (last 10)
            report["recent_diagnoses"] = list(diagnoses)[-10:]

        filename = LOG_DIR / f"healer_report_{now.strftime('%Y%m%d')}.json"
        with open(filename, "w") as f:
            json.dump(report, f, indent=2)

        emit_event("INFO", "healer", f"Daily report written: {filename}")
        return report

    except Exception as e:
        log.error("Failed to generate daily report: %s", e)
        return None


# ── Main Health Check Loop ────────────────────────────────────────────

def _check_with_timeout(svc, timeout=15):
    """Run health_check in a thread with hard timeout."""
    result = [False, "check timed out"]
    def target():
        result[0], result[1] = health_check(svc)
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout=timeout)
    return result[0], result[1]


async def run_health_checks_async():
    """Execute health checks concurrently using asyncio."""
    with lock:
        svc_snapshot = list(services.values())

    async def check_one(svc):
        now_str = datetime.now(timezone.utc).isoformat()
        # Run the existing health_check in a thread to avoid blocking
        ok, detail = await asyncio.to_thread(_check_with_timeout, svc, 15)

        with lock:
            svc.last_check = now_str
            if ok:
                if svc.status != "healthy":
                    emit_event("INFO", svc.name, f"Service recovered: {detail}")
                svc.status = "healthy"
                svc.last_healthy = now_str
                svc.consecutive_failures = 0
                svc.escalation_level = 0
            else:
                svc.consecutive_failures += 1
                svc.last_error = detail

                if svc.consecutive_failures >= 3:
                    svc.status = "down"
                    if svc.escalation_level < MAX_RESTART_ATTEMPTS:
                        restart_container(svc, reason=detail)
                elif svc.consecutive_failures >= 1:
                    svc.status = "degraded"
                    emit_event("WARN", svc.name, f"Degraded: {detail}")

        # Worker stuck detection
        if svc.svc_type == "worker" and ok:
            check_worker_stuck(svc)

    # Run all health checks concurrently
    await asyncio.gather(*[check_one(svc) for svc in svc_snapshot])


def run_health_checks():
    """Execute health checks — async if possible, sync fallback."""
    try:
        asyncio.run(run_health_checks_async())
    except Exception as e:
        log.error("Async health checks failed, running sync fallback: %s", e)
        _run_health_checks_sync()


def _run_health_checks_sync():
    """Sync fallback for health checks."""
    with lock:
        svc_snapshot = list(services.values())

    for svc in svc_snapshot:
        now_str = datetime.now(timezone.utc).isoformat()
        ok, detail = _check_with_timeout(svc, timeout=15)

        with lock:
            svc.last_check = now_str
            if ok:
                if svc.status != "healthy":
                    emit_event("INFO", svc.name, f"Service recovered: {detail}")
                svc.status = "healthy"
                svc.last_healthy = now_str
                svc.consecutive_failures = 0
                svc.escalation_level = 0
            else:
                svc.consecutive_failures += 1
                svc.last_error = detail

                if svc.consecutive_failures >= 3:
                    svc.status = "down"
                    if svc.escalation_level < MAX_RESTART_ATTEMPTS:
                        restart_container(svc, reason=detail)
                elif svc.consecutive_failures >= 1:
                    svc.status = "degraded"
                    emit_event("WARN", svc.name, f"Degraded: {detail}")

        if svc.svc_type == "worker" and ok:
            check_worker_stuck(svc)


# ── Embedded Sneakernet UI ─────────────────────────────────────────────

FLEET_UI_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ZOVARK FLEET COMMAND</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #060A14;
    color: #C8D6E5;
    font-family: 'Courier New', monospace;
    min-height: 100vh;
    overflow-x: hidden;
  }
  .header {
    background: linear-gradient(180deg, #0D1117 0%, #060A14 100%);
    border-bottom: 1px solid #1B2432;
    padding: 20px 30px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .header h1 {
    font-size: 18px;
    letter-spacing: 6px;
    text-transform: uppercase;
    color: #00FF88;
    text-shadow: 0 0 20px rgba(0,255,136,0.3);
  }
  .header .uptime {
    font-size: 12px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: #5A6A7A;
  }
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    padding: 20px 30px;
    max-width: 1600px;
    margin: 0 auto;
  }
  .card {
    background: #0D1117;
    border: 1px solid #1B2432;
    border-radius: 8px;
    padding: 20px;
    position: relative;
    overflow: hidden;
  }
  .card::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 2px;
    background: linear-gradient(90deg, transparent, #1B2432, transparent);
  }
  .card-title {
    font-size: 11px;
    letter-spacing: 4px;
    text-transform: uppercase;
    color: #5A6A7A;
    margin-bottom: 16px;
    padding-bottom: 8px;
    border-bottom: 1px solid #1B2432;
  }
  .full-width { grid-column: 1 / -1; }

  /* Traffic Lights */
  .service-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 12px;
  }
  .service-item {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px;
    background: #0A0E18;
    border: 1px solid #1B2432;
    border-radius: 6px;
    transition: border-color 0.3s;
  }
  .service-item:hover {
    border-color: #2B3442;
  }
  .light {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    flex-shrink: 0;
    box-shadow: 0 0 8px currentColor;
  }
  .light.green  { background: #00FF88; color: #00FF88; }
  .light.amber  { background: #FFAA00; color: #FFAA00; }
  .light.red    { background: #FF4444; color: #FF4444; }
  .light.gray   { background: #3A4A5A; color: #3A4A5A; }
  .svc-info { flex: 1; min-width: 0; }
  .svc-name {
    font-size: 12px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: #E0E8F0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .svc-detail {
    font-size: 10px;
    color: #5A6A7A;
    margin-top: 2px;
  }
  .svc-restarts {
    font-size: 10px;
    letter-spacing: 1px;
    text-transform: uppercase;
    padding: 2px 6px;
    border-radius: 3px;
    background: #1B2432;
    color: #5A6A7A;
  }
  .svc-restarts.active { background: #3A1A1A; color: #FF4444; }

  /* Event Log */
  .event-log {
    max-height: 400px;
    overflow-y: auto;
    font-size: 11px;
    line-height: 1.8;
  }
  .event-log::-webkit-scrollbar { width: 6px; }
  .event-log::-webkit-scrollbar-track { background: #0A0E18; }
  .event-log::-webkit-scrollbar-thumb { background: #1B2432; border-radius: 3px; }
  .event-row {
    display: flex;
    gap: 12px;
    padding: 4px 8px;
    border-bottom: 1px solid #0A0E18;
  }
  .event-row:hover { background: #0A0E18; }
  .evt-time { color: #3A5A7A; white-space: nowrap; flex-shrink: 0; width: 80px; }
  .evt-level { width: 70px; flex-shrink: 0; letter-spacing: 1px; text-transform: uppercase; font-weight: bold; }
  .evt-level.INFO { color: #00FF88; }
  .evt-level.WARN { color: #FFAA00; }
  .evt-level.ERROR { color: #FF4444; }
  .evt-level.CRITICAL { color: #FF4444; text-shadow: 0 0 10px rgba(255,68,68,0.5); }
  .evt-svc { color: #5A8ABF; width: 150px; flex-shrink: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .evt-msg { color: #C8D6E5; flex: 1; }

  /* Diagnoses */
  .diagnosis-item {
    padding: 12px;
    margin-bottom: 8px;
    background: #0A0E18;
    border: 1px solid #1B2432;
    border-radius: 6px;
    border-left: 3px solid #FFAA00;
  }
  .diag-header {
    display: flex;
    justify-content: space-between;
    margin-bottom: 8px;
  }
  .diag-svc {
    font-size: 12px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: #FFAA00;
  }
  .diag-time {
    font-size: 10px;
    color: #5A6A7A;
  }
  .diag-text {
    font-size: 11px;
    color: #C8D6E5;
    line-height: 1.6;
    white-space: pre-wrap;
  }

  /* Update Zone */
  .drop-zone {
    border: 2px dashed #1B2432;
    border-radius: 8px;
    padding: 40px;
    text-align: center;
    transition: border-color 0.3s, background 0.3s;
    cursor: pointer;
  }
  .drop-zone:hover, .drop-zone.dragover {
    border-color: #00FF88;
    background: rgba(0, 255, 136, 0.03);
  }
  .drop-label {
    font-size: 12px;
    letter-spacing: 3px;
    text-transform: uppercase;
    color: #5A6A7A;
  }
  .drop-hint {
    font-size: 10px;
    color: #3A4A5A;
    margin-top: 8px;
  }

  /* Summary bar */
  .summary-bar {
    display: flex;
    gap: 24px;
    padding: 12px 30px;
    background: #0D1117;
    border-bottom: 1px solid #1B2432;
  }
  .summary-stat {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .summary-count {
    font-size: 20px;
    font-weight: bold;
  }
  .summary-label {
    font-size: 10px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: #5A6A7A;
  }
  .count-green { color: #00FF88; }
  .count-amber { color: #FFAA00; }
  .count-red { color: #FF4444; }
  .count-gray { color: #5A6A7A; }

  .refresh-badge {
    font-size: 10px;
    letter-spacing: 1px;
    text-transform: uppercase;
    color: #3A5A7A;
    text-align: right;
    padding: 8px 30px;
  }

  @media (max-width: 900px) {
    .grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div class="header">
  <h1>ZOVARK FLEET COMMAND</h1>
  <div class="uptime" id="uptime">INITIALIZING...</div>
</div>

<div class="summary-bar" id="summary-bar">
  <div class="summary-stat"><span class="summary-count count-green" id="cnt-healthy">-</span><span class="summary-label">HEALTHY</span></div>
  <div class="summary-stat"><span class="summary-count count-amber" id="cnt-degraded">-</span><span class="summary-label">DEGRADED</span></div>
  <div class="summary-stat"><span class="summary-count count-red" id="cnt-down">-</span><span class="summary-label">DOWN</span></div>
  <div class="summary-stat"><span class="summary-count count-gray" id="cnt-unknown">-</span><span class="summary-label">UNKNOWN</span></div>
</div>

<div class="grid">

  <!-- Panel 1: System Health -->
  <div class="card">
    <div class="card-title">SYSTEM HEALTH</div>
    <div class="service-grid" id="service-grid">
      <div class="service-item">
        <div class="light gray"></div>
        <div class="svc-info"><div class="svc-name">LOADING...</div></div>
      </div>
    </div>
  </div>

  <!-- Panel 3: Update Zone -->
  <div class="card">
    <div class="card-title">UPDATE ZONE</div>
    <div class="drop-zone" id="drop-zone">
      <div class="drop-label">DROP .ZVK BUNDLE HERE</div>
      <div class="drop-hint">Air-gapped update delivery via sneakernet</div>
      <div class="drop-hint" style="margin-top:16px; color: #3A4A5A;">(STUB — bundle processing not yet implemented)</div>
    </div>

    <div class="card-title" style="margin-top:24px;">AI DIAGNOSES</div>
    <div id="diagnoses-list">
      <div class="diagnosis-item" style="border-left-color: #3A4A5A;">
        <div class="diag-text" style="color: #5A6A7A;">No diagnoses yet. Crash analysis will appear here when triggered.</div>
      </div>
    </div>
  </div>

  <!-- Panel 2: Self-Heal Log -->
  <div class="card full-width">
    <div class="card-title">SELF-HEAL LOG</div>
    <div class="event-log" id="event-log">
      <div class="event-row">
        <span class="evt-time">--:--:--</span>
        <span class="evt-level INFO">INFO</span>
        <span class="evt-svc">HEALER</span>
        <span class="evt-msg">Waiting for first health check cycle...</span>
      </div>
    </div>
  </div>

</div>

<div class="refresh-badge" id="refresh-badge">AUTO-REFRESH: 10S</div>

<script>
(function() {
  const API = window.location.origin + '/api';

  function lightClass(status) {
    if (status === 'healthy') return 'green';
    if (status === 'degraded') return 'amber';
    if (status === 'down') return 'red';
    return 'gray';
  }

  function shortTime(iso) {
    if (!iso) return '--:--:--';
    try {
      const d = new Date(iso);
      return d.toLocaleTimeString('en-US', {hour12: false});
    } catch(e) { return iso; }
  }

  function fmtUptime(seconds) {
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    let s = '';
    if (d > 0) s += d + 'D ';
    s += h + 'H ' + m + 'M';
    return 'UPTIME: ' + s;
  }

  async function fetchJSON(url) {
    const r = await fetch(url);
    return r.json();
  }

  async function refresh() {
    try {
      const [health, eventsData, diagData] = await Promise.all([
        fetchJSON(API + '/health'),
        fetchJSON(API + '/events'),
        fetchJSON(API + '/diagnoses'),
      ]);

      // Uptime
      if (health.uptime_seconds != null) {
        document.getElementById('uptime').textContent = fmtUptime(health.uptime_seconds);
      }

      // Summary counts
      let counts = {healthy:0, degraded:0, down:0, unknown:0};
      const svcs = health.services || {};
      Object.values(svcs).forEach(s => {
        const b = counts[s.status] !== undefined ? s.status : 'unknown';
        counts[b]++;
      });
      document.getElementById('cnt-healthy').textContent = counts.healthy;
      document.getElementById('cnt-degraded').textContent = counts.degraded;
      document.getElementById('cnt-down').textContent = counts.down;
      document.getElementById('cnt-unknown').textContent = counts.unknown;

      // Service grid
      const grid = document.getElementById('service-grid');
      const keys = Object.keys(svcs).sort();
      grid.innerHTML = keys.map(k => {
        const s = svcs[k];
        const lc = lightClass(s.status);
        const rc = s.restart_count > 0 ? ' active' : '';
        return '<div class="service-item">' +
          '<div class="light ' + lc + '"></div>' +
          '<div class="svc-info">' +
            '<div class="svc-name">' + escHtml(s.container_name || k) + '</div>' +
            '<div class="svc-detail">' + escHtml(s.type || '') + ' &middot; ' + shortTime(s.last_check) + '</div>' +
          '</div>' +
          '<span class="svc-restarts' + rc + '">' + (s.restart_count||0) + ' RST</span>' +
        '</div>';
      }).join('');

      // Event log
      const log = document.getElementById('event-log');
      const evts = (eventsData.events || []).slice(-80).reverse();
      log.innerHTML = evts.map(e => {
        return '<div class="event-row">' +
          '<span class="evt-time">' + shortTime(e.timestamp) + '</span>' +
          '<span class="evt-level ' + (e.level||'INFO') + '">' + (e.level||'INFO') + '</span>' +
          '<span class="evt-svc">' + escHtml(e.service||'') + '</span>' +
          '<span class="evt-msg">' + escHtml(e.message||'') + '</span>' +
        '</div>';
      }).join('') || '<div class="event-row"><span class="evt-msg" style="color:#5A6A7A">No events yet</span></div>';

      // Diagnoses
      const dlist = document.getElementById('diagnoses-list');
      const diags = (diagData.diagnoses || []).slice(-10).reverse();
      if (diags.length > 0) {
        dlist.innerHTML = diags.map(d => {
          return '<div class="diagnosis-item">' +
            '<div class="diag-header">' +
              '<span class="diag-svc">' + escHtml(d.service||'') + '</span>' +
              '<span class="diag-time">' + shortTime(d.timestamp) + '</span>' +
            '</div>' +
            '<div class="diag-text">' + escHtml(d.diagnosis||'') + '</div>' +
          '</div>';
        }).join('');
      }

      document.getElementById('refresh-badge').textContent =
        'LAST UPDATE: ' + new Date().toLocaleTimeString('en-US', {hour12:false}) + ' \u2022 AUTO-REFRESH: 10S';

    } catch(err) {
      document.getElementById('refresh-badge').textContent = 'FETCH ERROR: ' + err.message;
    }
  }

  function escHtml(s) {
    const el = document.createElement('span');
    el.textContent = s;
    return el.innerHTML;
  }

  // Drop zone stub
  const dz = document.getElementById('drop-zone');
  dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('dragover'); });
  dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
  dz.addEventListener('drop', e => {
    e.preventDefault();
    dz.classList.remove('dragover');
    const files = e.dataTransfer.files;
    if (files.length > 0) {
      alert('Received: ' + files[0].name + '\n\nBundle processing is not yet implemented.\nThis is a stub for future sneakernet update delivery.');
    }
  });

  refresh();
  setInterval(refresh, 10000);
})();
</script>
</body>
</html>"""


# ── HTTP Status Server ────────────────────────────────────────────────

class StatusHandler(BaseHTTPRequestHandler):
    """Serves the Sneakernet UI and JSON API endpoints."""

    def log_message(self, format, *args):
        """Suppress default HTTP logging."""
        pass

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.rstrip("/")

        if path == "" or path == "/":
            self._send_html(FLEET_UI_HTML)

        elif path == "/api/health" or path == "/health":
            with lock:
                svc_data = {name: svc.to_dict() for name, svc in services.items()}
            self._send_json({
                "status": "operational",
                "uptime_seconds": int(time.time() - healer_start_time),
                "check_interval": CHECK_INTERVAL,
                "services": svc_data,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        elif path == "/api/events" or path == "/events":
            with lock:
                evt_list = list(events)
            self._send_json({"events": evt_list, "count": len(evt_list)})

        elif path == "/api/diagnoses" or path == "/diagnoses":
            with lock:
                diag_list = list(diagnoses)
            self._send_json({"diagnoses": diag_list, "count": len(diag_list)})

        elif path == "/api/report" or path == "/report":
            report = generate_daily_report()
            if report:
                self._send_json(report)
            else:
                self._send_json({"error": "failed to generate report"}, 500)

        else:
            self._send_json({"error": "not found"}, 404)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def start_api_server():
    """Start the status API server in a background thread."""
    server = ThreadedHTTPServer(("0.0.0.0", API_PORT), StatusHandler)
    log.info("Status API + Sneakernet UI listening on http://0.0.0.0:%d", API_PORT)
    server.serve_forever()


# ── Main Loop ─────────────────────────────────────────────────────────

def probe_socket_proxy_reachability():
    """telemetry-audit-fix D5: startup readiness probe for docker-socket-proxy.

    Bound-after-HTTP-server-started: the API thread is already running by the
    time this runs, so :8081/api/health is reachable regardless of the probe
    outcome. Purpose is purely diagnostic — operators reading `docker compose
    logs healer` get a clear single-line success/failure signal instead of
    chasing a silent hang.

    Retries with exponential backoff (2 → 4 → 8 → 16 seconds, max 10 attempts,
    ~30s total budget). On persistent failure, sys.exit(1) so the compose
    `restart: unless-stopped` policy recycles the container.
    """
    docker_host = os.environ.get("DOCKER_HOST", "")
    if not docker_host:
        log.info("[healer] DOCKER_HOST unset; skipping socket-proxy probe")
        return

    # Expected shape: tcp://docker-socket-proxy:2375
    import urllib.request
    import urllib.error
    if docker_host.startswith("tcp://"):
        host_port = docker_host[len("tcp://"):]
    else:
        log.info("[healer] DOCKER_HOST=%s is not tcp://, skipping probe", docker_host)
        return

    probe_url = f"http://{host_port}/_ping"
    delays = [2, 4, 8, 16, 2, 4, 8, 16, 2, 4]  # 10 attempts, ~66s worst case
    for attempt, delay in enumerate(delays, 1):
        try:
            with urllib.request.urlopen(probe_url, timeout=5) as resp:
                body = resp.read(16).decode("utf-8", errors="replace").strip()
                if resp.status == 200 and body.lower() == "ok":
                    log.info("[healer] docker socket proxy reachable at %s (attempt %d)",
                             docker_host, attempt)
                    return
                log.warning("[healer] docker socket proxy probe: HTTP %d body=%r",
                            resp.status, body)
        except urllib.error.HTTPError as e:
            log.warning("[healer] docker socket proxy HTTP error (attempt %d/%d): %s",
                        attempt, len(delays), e)
        except urllib.error.URLError as e:
            log.warning("[healer] docker socket proxy unreachable (attempt %d/%d): %s",
                        attempt, len(delays), e.reason)
        except Exception as e:  # pragma: no cover
            log.warning("[healer] docker socket proxy probe error (attempt %d/%d): %s",
                        attempt, len(delays), e)
        if attempt < len(delays):
            time.sleep(delay)

    log.error("[healer] docker socket proxy never became reachable — exiting so "
              "compose restart policy recycles the container")
    sys.exit(1)


def main():
    """Entry point — service discovery, health checks, auto-heal."""
    log.info("=" * 60)
    log.info("  ZOVARK FLEET AGENT — Self-Healing Daemon v1.1")
    log.info("  Check interval: %ds", CHECK_INTERVAL)
    log.info("  LLM endpoint: %s (model: %s)", LLM_HOST, LLM_MODEL)
    log.info("  OTEL/Signoz checks: %s", "enabled" if OTEL_ENABLED else "disabled")
    log.info("  Status UI: http://0.0.0.0:%d", API_PORT)
    log.info("=" * 60)

    # Ensure log directory exists
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Start API server in background — must bind before health checks start.
    # telemetry-audit-fix D5: the HTTP server binds FIRST so /api/health is
    # reachable even while the socket-proxy probe below is retrying.
    api_thread = threading.Thread(target=start_api_server, daemon=True)
    api_thread.start()
    time.sleep(2)  # Let API thread bind port before health checks block

    # telemetry-audit-fix D5: diagnostic probe of the docker-socket-proxy
    # handshake. Logs a clear success/failure line so operators reading
    # `docker compose logs healer` don't chase a silent hang.
    probe_socket_proxy_reachability()

    emit_event("INFO", "healer", "Fleet Agent started",
               f"interval={CHECK_INTERVAL}s, llm={LLM_MODEL}")

    # Initial service discovery
    init_services()

    last_discovery = time.time()
    last_daily_report = 0
    last_disk_check = 0
    last_synthetic_login = 0
    last_connectivity_check = 0
    cycle = 0

    while True:
        cycle += 1

        # Re-discover services every 5 minutes
        if time.time() - last_discovery > 300:
            init_services()
            last_discovery = time.time()

        # Run health checks
        run_health_checks()

        # Connectivity checks every 60 seconds (API→DB, Worker→LLM)
        if time.time() - last_connectivity_check > CONNECTIVITY_CHECK_INTERVAL:
            check_connectivity()
            last_connectivity_check = time.time()

        # Synthetic login check every 60 seconds (Dashboard→API)
        if time.time() - last_synthetic_login > SYNTHETIC_LOGIN_INTERVAL:
            check_synthetic_login()
            last_synthetic_login = time.time()

        # Disk pressure check every 5 minutes
        if time.time() - last_disk_check > 300:
            check_disk_pressure()
            last_disk_check = time.time()

        # Daily report at midnight UTC (check every cycle, write once per day)
        now = datetime.now(timezone.utc)
        today_key = int(now.strftime("%Y%m%d"))
        if today_key > last_daily_report:
            # Also generate on first cycle
            if cycle > 1 or last_daily_report == 0:
                generate_daily_report()
                last_daily_report = today_key

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
