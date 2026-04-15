"""
OpenTelemetry tracing for Zovark worker.

Provides distributed tracing for the investigation pipeline.
Exports traces via OTLP HTTP to Signoz (self-hosted, ClickHouse-backed).

Usage:
    from tracing import tracer, trace_enabled

    if trace_enabled:
        with tracer.start_as_current_span("my_operation") as span:
            span.set_attribute("key", "value")
            ...

All tracing is optional — if OTEL_ENABLED=false or the collector is unreachable,
the pipeline works identically. Tracing never blocks or fails investigations.
"""
import atexit
import os
import threading
from contextlib import contextmanager

try:
    from settings import settings
    OTEL_ENABLED = settings.otel_enabled
    OTEL_ENDPOINT = settings.otel_endpoint.rstrip("/")
except Exception:
    OTEL_ENABLED = os.environ.get("OTEL_ENABLED", "false").lower() in ("1", "true", "yes")
    OTEL_ENDPOINT = os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT", "http://zovark-signoz-collector:4318"
    ).rstrip("/")

# Sentinel for disabled tracing
trace_enabled = False
tracer = None
_otel_log_provider = None  # shutdown flush for SigNoz
_otel_logging_initialized = False

# Audit 2.29: guard lazy init against concurrent callers. Two pipeline stages
# calling get_tracer() simultaneously could previously race and register two
# TracerProviders. Also register the trace provider's shutdown() via atexit so
# BatchSpanProcessor flushes pending spans on worker exit.
_init_lock = threading.Lock()
_trace_provider_for_shutdown = None


class _NoOpSpan:
    """No-op span for when tracing is disabled."""
    def set_attribute(self, key, value): pass
    def set_status(self, *args, **kwargs): pass
    def record_exception(self, exc): pass
    def add_event(self, name, attributes=None): pass
    def end(self): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass


class _NoOpTracer:
    """No-op tracer for when tracing is disabled."""
    def start_as_current_span(self, name, **kwargs):
        return _NoOpSpan()

    def start_span(self, name, **kwargs):
        return _NoOpSpan()


@contextmanager
def _noop_span_context(name, **kwargs):
    yield _NoOpSpan()


def init_tracing():
    """Initialize OpenTelemetry tracing. Safe to call even if deps missing."""
    global tracer, trace_enabled

    if not OTEL_ENABLED:
        tracer = _NoOpTracer()
        trace_enabled = False
        return tracer

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create({
            "service.name": "zovark-worker",
            "service.version": "3.0.0",
            "deployment.environment": os.environ.get("ZOVARK_ENV", "development"),
        })

        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=f"{OTEL_ENDPOINT}/v1/traces")
        provider.add_span_processor(BatchSpanProcessor(
            exporter,
            max_queue_size=2048,
            max_export_batch_size=512,
            schedule_delay_millis=5000,
        ))
        trace.set_tracer_provider(provider)

        # Audit 2.29: register shutdown() at atexit so spans in the
        # BatchSpanProcessor queue flush on graceful worker exit. Guard against
        # double-registration when get_tracer() is called repeatedly.
        global _trace_provider_for_shutdown
        if _trace_provider_for_shutdown is None:
            _trace_provider_for_shutdown = provider

            def _shutdown_trace_provider():
                try:
                    if _trace_provider_for_shutdown is not None:
                        _trace_provider_for_shutdown.shutdown()
                except Exception:
                    pass

            atexit.register(_shutdown_trace_provider)

        try:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

            HTTPXClientInstrumentor().instrument(tracer_provider=provider)
            print("[OTEL] httpx instrumented (Ticket 9)", flush=True)
        except Exception as he:
            print(f"[OTEL] httpx instrumentation skipped: {he}", flush=True)

        try:
            from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor

            Psycopg2Instrumentor().instrument(
                tracer_provider=provider,
                enable_commenter=True,
            )
            print("[OTEL] psycopg2 instrumented with SQL commenter (Ticket 9)", flush=True)
        except Exception as pe:
            print(f"[OTEL] psycopg2 instrumentation skipped: {pe}", flush=True)

        try:
            from opentelemetry.instrumentation.redis import RedisInstrumentor

            RedisInstrumentor().instrument(tracer_provider=provider)
            print("[OTEL] redis instrumented (Ticket 10)", flush=True)
        except Exception as re_:
            print(f"[OTEL] redis instrumentation skipped: {re_}", flush=True)

        tracer = trace.get_tracer("zovark-worker", "3.0.0")
        trace_enabled = True
        print(f"[OTEL] Tracing enabled → {OTEL_ENDPOINT}", flush=True)
        init_otel_logging()
        try:
            from metrics import init_worker_metrics
            init_worker_metrics()
        except Exception as me:
            print(f"[OTEL] metrics hook failed (non-fatal): {me}", flush=True)
        return tracer

    except ImportError:
        print("[OTEL] opentelemetry-sdk not installed, tracing disabled", flush=True)
        tracer = _NoOpTracer()
        trace_enabled = False
        return tracer
    except Exception as e:
        print(f"[OTEL] Tracing init failed (non-fatal): {e}", flush=True)
        tracer = _NoOpTracer()
        trace_enabled = False
        init_otel_logging()
        return tracer


def _parse_log_level(name: str, default: int) -> int:
    import logging

    return getattr(logging, name.upper(), None) or default


def init_otel_logging():
    """Export worker logs to SigNoz via OTLP HTTP (/v1/logs).

    Attaches OpenTelemetry's LoggingHandler to the **root** logger so every
    ``logging.getLogger(__name__)`` line propagates to SigNoz (not only
    ``zovark_worker``, which previously missed almost all pipeline logs).
    """
    global _otel_log_provider, _otel_logging_initialized
    if not OTEL_ENABLED or _otel_logging_initialized:
        return
    try:
        import atexit
        import logging

        from opentelemetry._logs import set_logger_provider
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create({
            "service.name": "zovark-worker",
            "service.version": "3.2.1",
            "deployment.environment": os.environ.get("ZOVARK_ENV", "development"),
        })
        provider = LoggerProvider(resource=resource)
        provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=f"{OTEL_ENDPOINT}/v1/logs"),
                max_export_batch_size=64,
                schedule_delay_millis=500,
                export_timeout_millis=10_000,
            )
        )
        set_logger_provider(provider)
        _otel_log_provider = provider

        root = logging.getLogger()
        otlp_endpoint_marker = f"{OTEL_ENDPOINT}/v1/logs"

        level_name = os.environ.get("ZOVARK_LOG_LEVEL", "INFO")
        root_level = _parse_log_level(level_name, logging.INFO)
        root.setLevel(root_level)

        handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)
        root.addHandler(handler)
        _otel_logging_initialized = True

        # logger.py sets zovark_worker.propagate = False — send those records to root/SigNoz too
        zw = logging.getLogger("zovark_worker")
        zw.propagate = True
        zw.setLevel(min(root_level, logging.DEBUG))

        # Third-party noise (still on stderr if configured; OTLP at WARNING+)
        for noisy in ("urllib3", "kafka", "kafka.conn", "httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        def _flush_logs():
            try:
                provider.shutdown()
            except Exception:
                pass

        atexit.register(_flush_logs)

        print(
            f"[OTEL] Log export enabled (root logger → {otlp_endpoint_marker}, level={level_name})",
            flush=True,
        )
    except ImportError:
        print("[OTEL] opentelemetry log exporter not available, log export skipped", flush=True)
    except Exception as e:
        print(f"[OTEL] Log export init failed (non-fatal): {e}", flush=True)


def get_tracer():
    """Get the global tracer (initializes on first call, thread-safe)."""
    global tracer
    if tracer is None:
        with _init_lock:
            if tracer is None:
                init_tracing()
    return tracer
