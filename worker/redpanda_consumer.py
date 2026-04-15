"""
Redpanda (Kafka API) consumer for canonical task dispatch.

Subscribes to topics `tasks.new.{tenant_id}` and starts Temporal workflows.
Replaces the deprecated NATS-based alert path.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from contextlib import nullcontext
from typing import Any, Callable, Optional

from kafka import KafkaConsumer
from kafka.errors import KafkaError

import logger


def _brokers_list() -> list[str]:
    raw = os.environ.get("ZOVARK_REDPANDA_BROKERS", "").strip()
    if not raw:
        return []
    return [b.strip() for b in raw.split(",") if b.strip()]


class RedpandaTaskConsumer:
    """Background thread: poll Redpanda, start workflows on the asyncio loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop, temporal_client: Any):
        self._loop = loop
        self._client = temporal_client
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._consumer: Optional[KafkaConsumer] = None
        # fix-e2e-ingest-stall D3: track previously-assigned partitions so we
        # can log only when the assignment actually changes (not every poll).
        self._last_assignment: frozenset = frozenset()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="redpanda-task-consumer")
        self._thread.start()
        logger.info("Redpanda task consumer thread started")

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        if self._consumer is not None:
            try:
                self._consumer.close()
            except Exception:
                pass
            self._consumer = None
        logger.info("Redpanda task consumer shut down")

    def _run(self) -> None:
        hosts = _brokers_list()
        if not hosts:
            logger.warn("ZOVARK_REDPANDA_BROKERS not set — Redpanda task consumer idle")
            return
        # fix-e2e-ingest-stall D1: lower metadata refresh interval from the
        # kafka-python default of 300_000 ms. Pattern subscription only
        # discovers new tenant topics during a metadata refresh; with the
        # default, a freshly-created tasks.new.<tenant> topic could take up
        # to 5 minutes to be picked up — well past the e2e probe's 90s
        # Stage 3 budget. 10 seconds gives 9× headroom on the probe and
        # negligible broker chatter (one MetadataRequest per worker per 10s).
        metadata_max_age_ms = int(
            os.environ.get("ZOVARK_REDPANDA_METADATA_MAX_AGE_MS", "10000")
        )
        try:
            self._consumer = KafkaConsumer(
                bootstrap_servers=hosts,
                group_id=os.environ.get("ZOVARK_REDPANDA_CONSUMER_GROUP", "zovark-task-workers"),
                value_deserializer=lambda b: json.loads(b.decode("utf-8")),
                key_deserializer=lambda b: b.decode("utf-8") if b else None,
                auto_offset_reset="earliest",
                enable_auto_commit=True,
                consumer_timeout_ms=1500,
                metadata_max_age_ms=metadata_max_age_ms,
            )
            self._consumer.subscribe(pattern=r"^tasks\.new\..+$")
            # fix-e2e-ingest-stall D2: force an immediate metadata fetch so
            # any pre-existing tasks.new.* topics are discovered within the
            # first millisecond of startup, not after one full
            # metadata_max_age_ms interval.
            try:
                self._consumer.poll(timeout_ms=0)
            except Exception:
                pass  # non-blocking; broker may not be up yet, the loop will retry
            logger.info(
                "Redpanda consumer subscribed",
                pattern="^tasks.new..+$",
                metadata_max_age_ms=metadata_max_age_ms,
            )
        except KafkaError as e:
            logger.error("Redpanda consumer init failed", error=str(e))
            return

        while not self._stop.is_set():
            try:
                batches = self._consumer.poll(timeout_ms=1000)
                # fix-e2e-ingest-stall D3: log assignment changes once per
                # change (not every poll). Converts silent waiting into an
                # observable signal for "consumer subscribed but no topics
                # match yet" vs "consumer is processing".
                try:
                    current = frozenset(
                        f"{tp.topic}:{tp.partition}"
                        for tp in self._consumer.assignment()
                    )
                except Exception:
                    current = frozenset()
                if current != self._last_assignment:
                    topics = sorted({entry.split(":", 1)[0] for entry in current})
                    logger.info(
                        "Redpanda consumer assignment changed",
                        partitions=len(current),
                        topic_count=len(topics),
                        topics=",".join(topics) if topics else "(none)",
                    )
                    self._last_assignment = current

                for _tp, records in batches.items():
                    for record in records:
                        if self._stop.is_set():
                            break
                        self._handle_record(record.value)
            except Exception as e:
                logger.error("Redpanda poll error", error=str(e))
                if self._stop.wait(2):
                    break

    def _handle_record(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("schema") != "zovark.tasks.new.v1":
            logger.warn("Ignoring unknown task envelope schema", schema=payload.get("schema"))
            return
        task_id = payload.get("task_id")
        if not task_id:
            return
        wf_name = payload.get("workflow") or os.environ.get(
            "ZOVARK_WORKFLOW_VERSION", "InvestigationWorkflowV2"
        )
        task_type = payload.get("task_type") or "log_analysis"
        inp = payload.get("input")
        if not isinstance(inp, dict):
            inp = {}

        async def _go() -> None:
            try:
                await _start_workflow_with_otel_parent(
                    self._client,
                    wf_name,
                    task_type,
                    inp,
                    task_id,
                    payload,
                )
                logger.info(
                    "Started workflow from Redpanda",
                    workflow=wf_name,
                    task_id=task_id,
                )
            except Exception as e:
                err = str(e).lower()
                if "already started" in err or "workflow execution already started" in err:
                    logger.info("Workflow already running (dedup)", task_id=task_id)
                    return
                logger.error(
                    "Failed to start workflow from Redpanda message",
                    task_id=task_id,
                    error=str(e),
                )

        fut = asyncio.run_coroutine_threadsafe(_go(), self._loop)
        try:
            fut.result(timeout=120)
        except Exception as e:
            logger.error("Temporal start_workflow future failed", task_id=task_id, error=str(e))


async def _start_workflow_with_otel_parent(
    temporal_client: Any,
    wf_name: str,
    task_type: str,
    inp: dict,
    task_id: str,
    envelope: dict,
) -> None:
    """Attach API-injected W3C context, then record a consumer span around start_workflow."""
    try:
        from opentelemetry import context as otel_context
        from opentelemetry.propagate import extract
        from opentelemetry.trace import SpanKind

        from tracing import get_tracer, trace_enabled
    except Exception:
        await temporal_client.start_workflow(
            wf_name,
            {"task_type": task_type, "input": inp},
            id=f"task-{task_id}",
            task_queue="zovark-tasks",
        )
        return

    carrier: dict[str, str] = {}
    tp = envelope.get("traceparent")
    if isinstance(tp, str) and tp:
        carrier["traceparent"] = tp
    ts = envelope.get("tracestate")
    if isinstance(ts, str) and ts:
        carrier["tracestate"] = ts

    token = None
    if carrier:
        try:
            token = otel_context.attach(extract(carrier))
        except Exception:
            token = None

    span_cm: Any = nullcontext()
    if trace_enabled:
        try:
            span_cm = get_tracer().start_as_current_span(
                "redpanda.start_workflow",
                kind=SpanKind.CONSUMER,
            )
        except Exception:
            span_cm = nullcontext()

    try:
        with span_cm as span:
            if span is not None and hasattr(span, "set_attribute"):
                try:
                    span.set_attribute("zovark.task_id", task_id)
                    span.set_attribute("zovark.workflow", wf_name)
                    span.set_attribute("messaging.system", "kafka")
                except Exception:
                    pass
            await temporal_client.start_workflow(
                wf_name,
                {"task_type": task_type, "input": inp},
                id=f"task-{task_id}",
                task_queue="zovark-tasks",
            )
    finally:
        if token is not None:
            try:
                otel_context.detach(token)
            except Exception:
                pass


def start_redpanda_task_consumer(
    loop: asyncio.AbstractEventLoop,
    temporal_client: Any,
) -> Optional[Callable[[], None]]:
    """Start consumer if brokers configured; return shutdown callable or None."""
    if not _brokers_list():
        logger.info("Redpanda task consumer disabled (no brokers)")
        return None
    c = RedpandaTaskConsumer(loop, temporal_client)
    c.start()
    return c.shutdown
