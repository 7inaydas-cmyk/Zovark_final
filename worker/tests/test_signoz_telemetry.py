"""
SigNoz telemetry smoke — exercise API + worker OTel paths so traces/logs appear in SigNoz.

Prerequisites:
  docker compose --profile tracing up -d
  API and worker with OTEL_ENABLED=true (default in compose when profile is used).

Run inside compose network (recommended):
  docker compose run --rm worker pytest tests/test_signoz_telemetry.py -v --tb=short

From host (API on localhost):
  ZOVARK_API_URL=http://127.0.0.1:8090 pytest worker/tests/test_signoz_telemetry.py -v

Optional: verify SigNoz Query API (JWT from SigNoz UI → Settings → API keys / login):
  SIGNOZ_VERIFY=1 SIGNOZ_BASE=http://127.0.0.1:3301 SIGNOZ_JWT=<token> pytest ...

Cursor SigNoz MCP (same backend): after smoke traffic, call signoz_list_services,
signoz_search_traces_by_service(service=zovark-api), signoz_aggregate_traces(...).
See scripts/verify_signoz_telemetry_e2e.sh
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid

import httpx
import pytest

pytestmark = pytest.mark.integration

API_URL = os.environ.get("ZOVARK_API_URL", "http://zovark-api:8090").rstrip("/")
SIGNOZ_BASE = os.environ.get("SIGNOZ_BASE", "http://127.0.0.1:3301").rstrip("/")
ADMIN_EMAIL = os.environ.get("ZOVARK_SMOKE_EMAIL", "admin@test.local")
ADMIN_PASSWORD = os.environ.get("ZOVARK_SMOKE_PASSWORD", "TestPass2026")


def _api_client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(base_url=API_URL, timeout=timeout)


def test_smoke_emit_api_login_trace():
    """POST /api/v1/auth/login is traced (gin otel); failed login still hits handler."""
    run_id = str(uuid.uuid4())
    with _api_client() as c:
        r = c.post(
            "/api/v1/auth/login",
            json={
                "email": ADMIN_EMAIL,
                "password": "wrong-on-purpose-" + run_id[:8],
            },
        )
    assert r.status_code in (401, 403, 422), r.text


def test_smoke_emit_api_login_success_and_list_tasks():
    """Successful login + DB-backed list generates pgx + redis + HTTP spans."""
    _ = str(uuid.uuid4())
    with _api_client() as c:
        r = c.post(
            "/api/v1/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        if r.status_code != 200:
            pytest.skip(
                f"login returned {r.status_code} ({r.text[:200]}) — "
                "seed tenant user or set ZOVARK_SMOKE_EMAIL / ZOVARK_SMOKE_PASSWORD"
            )
        token = r.json().get("token") or r.json().get("access_token")
        assert token, r.json()
        h = {"Authorization": f"Bearer {token}"}
        r2 = c.get("/api/v1/tasks", params={"limit": 3}, headers=h)
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert isinstance(body, (list, dict))


def test_smoke_emit_api_metrics_path():
    """Prometheus scrape path (if exposed) — not always in trace pipeline."""
    with _api_client() as c:
        r = c.get("/metrics")
    # May be 404 if metrics disabled; 200 with text if enabled
    assert r.status_code in (200, 404), r.text


def test_smoke_emit_worker_otel_span_and_log():
    """In-process worker span + OTLP log line (zovark_worker logger)."""
    os.environ["OTEL_ENABLED"] = "true"
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        os.environ.setdefault(
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "http://zovark-signoz-collector:4318",
        )
    from tracing import init_tracing, trace_enabled

    init_tracing()
    if not trace_enabled:
        pytest.skip("OTEL not active (deps or OTEL_ENABLED=false at import time)")

    rid = str(uuid.uuid4())
    from tracing import get_tracer

    tracer = get_tracer()
    with tracer.start_as_current_span("signoz.smoke.worker_test") as span:
        span.set_attribute("signoz.smoke.run_id", rid)
        span.set_attribute("signoz.smoke.case", "worker_manual_span")
    log = logging.getLogger("zovark_worker")
    log.warning("signoz_smoke_log_line run_id=%s", rid)
    # Audit 5.14: force flush instead of sleeping. Sleeps are non-deterministic
    # on slow CI runners and mask slow exporters under the guise of "success".
    try:
        provider = getattr(tracer, "_provider", None)
        if provider is None:
            from opentelemetry import trace as _trace
            provider = _trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=5000)
    except Exception:  # pragma: no cover - best-effort
        pass


def test_smoke_emit_httpx_client_span_when_otel_on():
    """HTTPXClientInstrumentor should create client spans for outbound HTTP."""
    os.environ["OTEL_ENABLED"] = "true"
    os.environ.setdefault(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or "http://zovark-signoz-collector:4318",
    )
    from tracing import init_tracing, trace_enabled

    init_tracing()
    if not trace_enabled:
        pytest.skip("OTEL not active")

    rid = str(uuid.uuid4())
    from tracing import get_tracer

    tracer = get_tracer()
    with tracer.start_as_current_span("signoz.smoke.httpx_parent") as span:
        span.set_attribute("signoz.smoke.run_id", rid)
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{API_URL}/health")
            assert r.status_code == 200


def test_smoke_correlation_header_present_on_api():
    """X-Zovark-Trace-ID is set on API responses when OTEL middleware runs."""
    with _api_client() as c:
        r = c.post(
            "/api/v1/auth/login",
            json={"email": ADMIN_EMAIL, "password": "x"},
        )
    tid = r.headers.get("x-zovark-trace-id") or r.headers.get("X-Zovark-Trace-ID")
    assert tid, f"missing trace header: {dict(r.headers)}"


def test_smoke_otlp_collector_tcp_reachable():
    """OTLP HTTP port on the in-cluster collector (traces/metrics/logs fan-in)."""
    import socket

    host = os.environ.get("ZOVARK_OTEL_COLLECTOR_HOST", "zovark-signoz-collector")
    port = int(os.environ.get("ZOVARK_OTEL_COLLECTOR_PORT", "4318"))
    try:
        with socket.create_connection((host, port), timeout=5):
            pass
    except OSError as e:
        pytest.skip(f"collector not reachable at {host}:{port}: {e}")


@pytest.mark.skipif(
    not os.environ.get("SIGNOZ_VERIFY", "").strip().lower() in ("1", "true", "yes"),
    reason="Set SIGNOZ_VERIFY=1 and SIGNOZ_JWT to query SigNoz API",
)
def test_signoz_api_expects_zovark_services():
    """E2E: SigNoz HTTP API lists zovark-api (and usually zovark-worker) after smoke traffic."""
    jwt = os.environ.get("SIGNOZ_JWT", "").strip()
    assert jwt, "SIGNOZ_JWT required"
    headers = {"Authorization": f"Bearer {jwt}"}
    with httpx.Client(timeout=30.0) as client:
        r = client.get(f"{SIGNOZ_BASE}/api/v1/services", headers=headers)
    if r.status_code == 404:
        pytest.skip("Signoz /api/v1/services not available on this build")
    assert r.status_code == 200, r.text
    data = r.json()
    names: list[str] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("serviceName"):
                names.append(str(item["serviceName"]))
            elif isinstance(item, str):
                names.append(item)
    elif isinstance(data, dict):
        for item in data.get("data") or data.get("services") or []:
            if isinstance(item, dict) and item.get("serviceName"):
                names.append(str(item["serviceName"]))
    assert "zovark-api" in names, f"expected zovark-api in services, got: {names[:20]!r}"
