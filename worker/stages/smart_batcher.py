"""
Smart Batcher — Aggregate similar alerts within a time window.

Batching key: hash(task_type + source_ip)
Window: 60s (configurable via ZOVARK_BATCH_WINDOW_SECONDS)
Max batch size: 500 (configurable via ZOVARK_BATCH_MAX_SIZE)

Redis-backed state with in-memory fallback if Redis is unavailable.
Module-level singleton via get_batcher(redis_client).
"""
import os
import json
import time
import hashlib
import threading
from typing import Optional, Tuple

BATCH_WINDOW_SECONDS = int(os.environ.get("ZOVARK_BATCH_WINDOW_SECONDS", "60"))
BATCH_MAX_SIZE = int(os.environ.get("ZOVARK_BATCH_MAX_SIZE", "500"))

# Severity multipliers for batch window — critical alerts have shorter windows
SEVERITY_WINDOW_MULTIPLIER = {
    "critical": 0.25,
    "high": 0.5,
    "medium": 1.0,
    "low": 2.0,
    "info": 3.0,
}


def _batch_key(tenant_id: str, task_type: str, source_ip: str) -> str:
    """Compute deterministic batch key from tenant + task_type + source_ip.

    Audit 2.12: tenant_id is mandatory — omitting it mixed alerts across tenants
    with the same (task_type, source_ip) into a single aggregated event, which
    leaked raw_log/username data across tenant boundaries.
    """
    if not tenant_id:
        raise ValueError("smart_batcher: tenant_id is required")
    raw = f"{tenant_id}:{task_type.lower().strip()}:{source_ip.strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class SmartBatcher:
    """
    Aggregates similar alerts within a configurable time window.

    When the first alert of a given (task_type, source_ip) pair arrives,
    it starts a batch window. Subsequent alerts within the window are absorbed.
    When the window expires (next alert arrives after window), the aggregated
    event is released for processing.

    State is stored in Redis (prefix 'batch:'). Falls back to in-memory dict
    if Redis is unavailable.
    """

    def __init__(self, redis_client=None):
        self._redis = redis_client
        self._use_redis = redis_client is not None
        # In-memory fallback: {batch_key: {"count": N, "first_ts": float, "events": [...]}}
        self._mem_store: dict = {}
        self._lock = threading.Lock()

    def _effective_window(self, severity: str) -> float:
        """Compute effective batch window based on severity."""
        mult = SEVERITY_WINDOW_MULTIPLIER.get(severity.lower(), 1.0)
        return BATCH_WINDOW_SECONDS * mult

    def should_batch(
        self,
        tenant_id: str,
        task_type: str,
        siem_event: dict,
        severity: str = "medium",
    ) -> Tuple[bool, Optional[dict]]:
        """
        Decide whether to absorb or release this alert.

        Returns:
            (should_skip, aggregated_event)
            - (True, None)       — alert absorbed into active batch, skip processing
            - (False, None)      — first alert, no batching needed, process normally
            - (False, aggregated) — batch window expired, process the aggregated event
        """
        source_ip = siem_event.get("source_ip") or ""
        if not source_ip:
            ep = siem_event.get("src_endpoint")
            if isinstance(ep, dict):
                ip = ep.get("ip")
                if isinstance(ip, str):
                    source_ip = ip
        # Audit 2.13: fall back from "unknown" to something meaningful so alerts
        # missing source_ip are still separated by rule_name / hostname and
        # don't all pile into a single mega-batch per tenant.
        if not source_ip:
            hostname = siem_event.get("hostname") or ""
            rule_name = siem_event.get("rule_name") or ""
            if hostname:
                source_ip = f"host:{hostname}"
            elif rule_name:
                source_ip = f"rule:{rule_name}"
            else:
                source_ip = "unknown"
        bkey = _batch_key(tenant_id, task_type, source_ip)
        now = time.time()
        window = self._effective_window(severity)

        if self._use_redis:
            return self._should_batch_redis(bkey, task_type, source_ip, siem_event, now, window)
        else:
            return self._should_batch_memory(bkey, task_type, source_ip, siem_event, now, window)

    # ------------------------------------------------------------------ Redis
    def _should_batch_redis(
        self, bkey: str, task_type: str, source_ip: str,
        siem_event: dict, now: float, window: float,
    ) -> Tuple[bool, Optional[dict]]:
        redis_key = f"batch:{bkey}"
        try:
            raw = self._redis.get(redis_key)
        except Exception:
            # Redis failed — fall back to memory for this call
            return self._should_batch_memory(bkey, task_type, source_ip, siem_event, now, window)

        if raw is None:
            # First alert — start a new batch window
            batch_data = {
                "count": 1,
                "first_ts": now,
                "task_type": task_type,
                "source_ip": source_ip,
                "events": [siem_event],
            }
            try:
                ttl = int(window) + 30  # Extra 30s grace for retrieval
                self._redis.setex(redis_key, ttl, json.dumps(batch_data, default=str))
            except Exception:
                pass
            return (False, None)

        # Existing batch
        try:
            batch_data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # Corrupted data — treat as first alert
            self._redis.delete(redis_key)
            return (False, None)

        first_ts = batch_data.get("first_ts", now)
        count = batch_data.get("count", 0)
        events = batch_data.get("events", [])

        # Check if window has expired
        if now - first_ts >= window:
            # Window expired — release the aggregated batch (including current alert)
            events.append(siem_event)
            aggregated = self._build_aggregated_event(
                task_type, source_ip, events, first_ts, now, window
            )
            try:
                self._redis.delete(redis_key)
            except Exception:
                pass
            return (False, aggregated)

        # Within window — absorb if under max size
        if count >= BATCH_MAX_SIZE:
            # Max size reached — release what we have
            events.append(siem_event)
            aggregated = self._build_aggregated_event(
                task_type, source_ip, events, first_ts, now, window
            )
            try:
                self._redis.delete(redis_key)
            except Exception:
                pass
            return (False, aggregated)

        # Absorb into batch
        events.append(siem_event)
        batch_data["count"] = count + 1
        batch_data["events"] = events
        try:
            remaining_ttl = int(window - (now - first_ts)) + 30
            self._redis.setex(redis_key, max(remaining_ttl, 10), json.dumps(batch_data, default=str))
        except Exception:
            pass
        return (True, None)

    # -------------------------------------------------------------- In-memory
    def _should_batch_memory(
        self, bkey: str, task_type: str, source_ip: str,
        siem_event: dict, now: float, window: float,
    ) -> Tuple[bool, Optional[dict]]:
        with self._lock:
            if bkey not in self._mem_store:
                # First alert
                self._mem_store[bkey] = {
                    "count": 1,
                    "first_ts": now,
                    "task_type": task_type,
                    "source_ip": source_ip,
                    "events": [siem_event],
                }
                return (False, None)

            batch = self._mem_store[bkey]
            first_ts = batch["first_ts"]
            events = batch["events"]

            # Window expired — release
            if now - first_ts >= window:
                events.append(siem_event)
                aggregated = self._build_aggregated_event(
                    task_type, source_ip, events, first_ts, now, window
                )
                del self._mem_store[bkey]
                return (False, aggregated)

            # Max size reached — release
            if batch["count"] >= BATCH_MAX_SIZE:
                events.append(siem_event)
                aggregated = self._build_aggregated_event(
                    task_type, source_ip, events, first_ts, now, window
                )
                del self._mem_store[bkey]
                return (False, aggregated)

            # Absorb
            events.append(siem_event)
            batch["count"] += 1
            return (True, None)

    # --------------------------------------------------- Aggregated event builder
    def _build_aggregated_event(
        self,
        task_type: str,
        source_ip: str,
        events: list,
        first_ts: float,
        now: float,
        window: float,
    ) -> dict:
        """
        Build an aggregated SIEM event from a batch of similar alerts.

        The aggregated event preserves all unique field values and adds
        batch metadata (_batched, _batch_count, _batch_window_seconds).
        """
        batch_count = len(events)

        # Start from the first event as the base
        base = dict(events[0]) if events else {}

        # Collect unique usernames, destination IPs, hostnames, raw_logs
        usernames = set()
        dest_ips = set()
        hostnames = set()
        raw_logs = []
        rule_names = set()
        severities = set()

        for evt in events:
            if evt.get("username"):
                usernames.add(evt["username"])
            if evt.get("destination_ip"):
                dest_ips.add(evt["destination_ip"])
            if evt.get("hostname"):
                hostnames.add(evt["hostname"])
            if evt.get("raw_log"):
                raw_logs.append(evt["raw_log"])
            if evt.get("rule_name"):
                rule_names.add(evt["rule_name"])
            if evt.get("severity"):
                severities.add(evt["severity"])

        # Build aggregated event
        aggregated = dict(base)
        aggregated["source_ip"] = source_ip

        if len(usernames) > 1:
            aggregated["username"] = ", ".join(sorted(usernames))
            aggregated["_unique_usernames"] = sorted(usernames)
        elif len(usernames) == 1:
            aggregated["username"] = usernames.pop()

        if len(dest_ips) > 1:
            aggregated["_unique_destination_ips"] = sorted(dest_ips)
        if len(hostnames) > 1:
            aggregated["_unique_hostnames"] = sorted(hostnames)
        if len(rule_names) > 1:
            aggregated["_unique_rule_names"] = sorted(rule_names)

        # Concatenate raw logs (cap at 10 to avoid oversized payloads)
        if len(raw_logs) > 1:
            capped = raw_logs[:10]
            aggregated["raw_log"] = "\n---\n".join(capped)
            if len(raw_logs) > 10:
                aggregated["raw_log"] += f"\n--- (+{len(raw_logs) - 10} more alerts)"

        # Pick highest severity
        severity_order = ["critical", "high", "medium", "low", "info"]
        if severities:
            for sev in severity_order:
                if sev in severities:
                    aggregated["severity"] = sev
                    break

        # Batch metadata
        aggregated["_batched"] = True
        aggregated["_batch_count"] = batch_count
        aggregated["_batch_window_seconds"] = window
        aggregated["_batch_first_ts"] = first_ts
        aggregated["_batch_last_ts"] = now

        return aggregated


# ----------------------------------------------------------------- Singleton
_batcher_instance: Optional[SmartBatcher] = None
_batcher_lock = threading.Lock()


def get_batcher(redis_client=None) -> SmartBatcher:
    """
    Module-level singleton. Pass redis_client on first call;
    subsequent calls reuse the same instance.
    """
    global _batcher_instance
    if _batcher_instance is None:
        with _batcher_lock:
            if _batcher_instance is None:
                _batcher_instance = SmartBatcher(redis_client=redis_client)
    return _batcher_instance
