"""
Stage 5: STORE — Persist all investigation artifacts.
NO LLM calls. DB writes only.

Self-contained: imports psycopg2 directly.
Does NOT import from _legacy_activities.py or entity_graph.py.
"""
import os
import json
import time
from contextlib import contextmanager
from dataclasses import asdict

import psycopg2

from temporalio import activity
from stages import StoreOutput
from stages.trace_helpers import trace_stage_store_span, trace_store_apply_outcome

# stabilize-runtime-hygiene: centralized DB/Redis URL via settings.
from settings import settings as _settings
DATABASE_URL = os.environ.get("DATABASE_URL", _settings.database_url)
FAST_FILL = os.environ.get("ZOVARK_FAST_FILL", "false").lower() == "true"


REDIS_URL = os.environ.get("REDIS_URL", _settings.redis_url)


def _get_redis():
    """Get a Redis connection for dedup entry updates. Returns None on failure."""
    try:
        import redis as _redis
        return _redis.from_url(REDIS_URL, decode_responses=True)
    except Exception:
        return None


def _update_dedup_entry(conn, task_id: str, verdict: str, risk_score: int, status: str, siem_event: dict):
    """Update the Redis dedup entry with investigation results (v2 dedup).
    Uses only agent_tasks.dedup_hash (API-computed). Never recomputes hashes in Python."""
    try:
        r = _get_redis()
        if r is None:
            return
        alert_hash = None
        try:
            with _savepoint(conn, "sp_update_dedup_entry"):
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT dedup_hash FROM agent_tasks WHERE id = %s",
                        (task_id,),
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        alert_hash = row[0]
        except Exception:
            pass
        if not alert_hash:
            print(
                f"[STORE] WARNING: agent_tasks.dedup_hash missing for task_id={task_id}; "
                "skipping Redis dedup entry update (API-only dedup hash ownership)"
            )
            r.close()
            return
        key = f"dedup:exact:{alert_hash}"
        existing = r.get(key)
        if existing:
            entry = json.loads(existing)
            if entry.get("task_id") == task_id:
                entry["status"] = "completed" if status == "completed" else "failed"
                entry["verdict"] = verdict
                entry["risk_score"] = risk_score
                ttl = r.ttl(key)
                if ttl and ttl > 0:
                    r.setex(key, ttl, json.dumps(entry))
        r.close()
    except Exception as e:
        print(f"Dedup entry update failed (non-fatal): {e}")


def _get_db():
    """Deprecated shim. Audit 2.3: new code should use `with _db_conn() as conn:`
    so the connection is returned to the pool on every exit path."""
    if _USE_POOL:
        from database.pool_manager import _pools
        pool = _pools.get("normal") or _pools.get("critical")
        if pool is not None:
            return pool.getconn()
    return psycopg2.connect(DATABASE_URL)


# Audit 2.3: route store_investigation through the pooled connection helper so
# the worker doesn't open a fresh socket on every investigation. Context
# manager ensures putconn() runs on both success and exception paths.
try:
    from database.pool_manager import pooled_connection as _pooled_connection  # noqa: F401
    _USE_POOL = True
except ImportError:
    _USE_POOL = False


@contextmanager
def _db_conn():
    """Yield a DB connection and return it to the pool on exit."""
    if _USE_POOL:
        from database.pool_manager import _pools
        pool = _pools.get("normal") or _pools.get("critical")
        if pool is not None:
            conn = pool.getconn()
            try:
                yield conn
            finally:
                try:
                    pool.putconn(conn)
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
            return
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


@contextmanager
def _savepoint(conn, name):
    """Wrap a block in a Postgres SAVEPOINT so cursor failures inside the block
    do not poison the parent transaction. On exception, ROLLBACK TO SAVEPOINT and
    re-raise so the surrounding try/except still observes the Python exception."""
    with conn.cursor() as cur:
        cur.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        try:
            with conn.cursor() as cur:
                cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
        except Exception:
            pass
        raise
    else:
        try:
            with conn.cursor() as cur:
                cur.execute(f"RELEASE SAVEPOINT {name}")
        except Exception:
            pass


# Audit 2.2: UUID validator for SET LOCAL app.current_tenant interpolation.
import uuid as _uuid  # noqa: E402


def validate_tenant_id(tenant_id: str) -> str:
    if not isinstance(tenant_id, str) or not tenant_id:
        raise ValueError("tenant_id must be a non-empty string")
    _uuid.UUID(tenant_id)
    return tenant_id


def _get_worker_id():
    import socket, random, string
    host = socket.gethostname()
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{host}-{suffix}"


# --- Task status update ---
def _update_task_status(conn, task_id: str, status: str, output: dict,
                        tokens_in: int = 0, tokens_out: int = 0,
                        execution_ms: int = 0, severity: str = None,
                        error_message: str = None, model_name: str = "unknown",
                        path_taken: str = "", generated_code: str = ""):
    """Update agent_tasks with final status. Uses synchronous_commit for durability."""
    worker_id = _get_worker_id()
    human_review_threshold = int(os.environ.get("ZOVARK_HUMAN_REVIEW_THRESHOLD", "60"))

    risk_score = 0
    if isinstance(output, dict):
        risk_score = output.get("risk_score", 0) or 0

    needs_review = False
    review_reason = None
    # Check if assess stage explicitly flagged for review (e.g., LLM down)
    if isinstance(output, dict) and output.get("needs_human_review"):
        needs_review = True
        review_reason = output.get("review_reason", "Flagged for manual review")
    elif status != "completed":
        needs_review = True
        review_reason = error_message or "Investigation failed"
    elif risk_score >= human_review_threshold:
        # FIX LOGIC-001: flag HIGH-risk investigations for review (was inverted — was using <)
        needs_review = True
        review_reason = f"Risk score {risk_score} at or above threshold {human_review_threshold}"

    with conn.cursor() as cur:
        # Critical write: ensure WAL flush before acknowledging
        cur.execute("SET LOCAL synchronous_commit = on;")
        cur.execute("""
            UPDATE agent_tasks
            SET status = %s, output = %s, error_message = %s,
                tokens_used_input = %s, tokens_used_output = %s, execution_ms = %s,
                severity = %s, worker_id = COALESCE(%s, worker_id),
                needs_human_review = %s, review_reason = %s,
                model_name = %s,
                path_taken = %s, generated_code = %s,
                completed_at = NOW()
            WHERE id = %s
        """, (
            status, json.dumps(output), error_message,
            tokens_in, tokens_out, execution_ms,
            severity, worker_id,
            needs_review, review_reason,
            model_name,
            path_taken or None, (generated_code or "")[:50000] if generated_code else None,
            task_id,
        ))


# --- Investigation memory ---
def _save_pattern(conn, task_type: str, alert_sig: str, code: str,
                  iocs: list, findings: list, risk_score: int, success: bool):
    """Save investigation pattern to memory table. No LLM."""
    try:
        with _savepoint(conn, "sp_save_pattern"):
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO investigation_memory
                    (task_type, alert_signature, code_template, iocs_found,
                     findings_found, risk_score, success)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    task_type, alert_sig, (code or "")[:10000],
                    json.dumps(iocs), json.dumps(findings),
                    risk_score, success,
                ))
    except Exception as e:
        print(f"Pattern save failed (non-fatal): {e}")


# --- Investigation row (no embedding) ---
def _create_investigation(conn, tenant_id: str, task_id: str, verdict: str,
                          risk_score: int, confidence: float, summary: str,
                          model_name: str = "unknown"):
    """Insert investigations row without embedding. Uses synchronous_commit for durability.
    Returns investigation_id."""
    try:
        with _savepoint(conn, "sp_create_investigation"):
            with conn.cursor() as cur:
                # Critical write: ensure WAL flush before acknowledging
                cur.execute("SET LOCAL synchronous_commit = on;")
                cur.execute("""
                    INSERT INTO investigations
                    (tenant_id, task_id, verdict, risk_score, confidence,
                     summary, source, model_name)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (tenant_id, task_id, verdict, risk_score, confidence,
                      (summary or "")[:2000], "production", model_name))
                row = cur.fetchone()
                return str(row[0]) if row else None
    except Exception as e:
        print(f"Investigation insert failed (non-fatal): {e}")
        return None


# --- Audit event ---
def _insert_audit_event(conn, tenant_id: str, event_type: str,
                        resource_type: str, resource_id: str,
                        metadata: dict = None, trace_id: str = ""):
    """Insert audit_events row. Non-fatal on failure."""
    try:
        with _savepoint(conn, "sp_insert_audit_event"):
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO audit_events
                    (tenant_id, event_type, actor_type, resource_type, resource_id, metadata, trace_id)
                    VALUES (%s, %s, 'worker', %s, %s, %s, %s)
                """, (
                    tenant_id, event_type, resource_type,
                    resource_id, json.dumps(metadata or {}),
                    trace_id or None,
                ))
    except Exception as e:
        print(f"Audit event insert failed (non-fatal): {e}")


# --- Severity from risk score (shared from worker/verdict.py) ---
from verdict import severity_from_risk as _severity_from_risk_impl


def _severity_from_risk(risk_score: int) -> str:
    return _severity_from_risk_impl(risk_score)


# --- Main entry point ---
@activity.defn
async def store_investigation(data: dict) -> dict:
    """
    Stage 5: Persist all investigation artifacts.
    NO LLM calls. DB writes only.

    Input: merged dict from AssessOutput + ExecuteOutput + task metadata
    Returns: dict (serializable StoreOutput fields)
    """
    span_ctx = {
        "task_id": data.get("task_id", "") or "",
        "tenant_id": data.get("tenant_id", "") or "",
        "task_type": data.get("task_type", "") or "",
        "trace_id": str(data.get("trace_id", "") or ""),
    }
    import time as _time
    _t0 = _time.perf_counter()
    try:
        with trace_stage_store_span(span_ctx) as _span:
            return await _store_investigation_body(data, _span)
    finally:
        try:
            from metrics import record_pipeline_stage
            record_pipeline_stage("store", _time.perf_counter() - _t0)
        except Exception:
            pass


async def _store_investigation_body(data: dict, _span) -> dict:
    task_id = data.get("task_id", "")
    tenant_id = data.get("tenant_id", "")
    status = data.get("status", "completed")
    verdict = data.get("verdict", "inconclusive")
    risk_score = data.get("risk_score", 0)
    confidence = data.get("confidence", 0.5)
    memory_summary = data.get("memory_summary", "")
    stdout = data.get("stdout", "")
    stderr = data.get("stderr", "")
    iocs = data.get("iocs", [])
    findings = data.get("findings", [])
    recommendations = data.get("recommendations", [])
    code = data.get("code", "")
    tokens_in = data.get("tokens_in", 0)
    tokens_out = data.get("tokens_out", 0)
    execution_ms = data.get("execution_ms", 0)
    task_type = data.get("task_type", "")
    model_name = data.get("model_name", "unknown")
    siem_event = data.get("siem_event", {})
    path_taken = data.get("path_taken", "")
    generated_code = data.get("generated_code", data.get("code", ""))
    trace_id = data.get("trace_id", "")

    severity = _severity_from_risk(risk_score)
    investigation_id = None

    # Audit 2.3: pooled connection (ctx manager returns it to the pool on exit).
    # Audit 2.2: tenant_id validated via uuid.UUID before SET LOCAL interpolation.
    with _db_conn() as conn:
      try:
        if tenant_id:
            try:
                validated_tid = validate_tenant_id(tenant_id)
                with conn.cursor() as cur:
                    cur.execute(f"SET LOCAL app.current_tenant = '{validated_tid}'")
            except ValueError as ve:
                activity.logger.error(
                    f"store_investigation: invalid tenant_id '{tenant_id}' ({ve}) — aborting"
                )
                return {"status": "error", "error": "invalid_tenant_id"}

        # 0. Audit: investigation_started
        if tenant_id:
            _insert_audit_event(
                conn, tenant_id, "investigation_started",
                "agent_task", task_id,
                {"task_type": task_type, "severity": severity, "model": model_name},
                trace_id=trace_id,
            )

        # 1. Update task status
        output = {
            "stdout": stdout,
            "iocs": iocs,
            "findings": findings,
            "risk_score": risk_score,
            "verdict": verdict,
            "recommendations": recommendations,
            "model_used": model_name,
            "stderr": stderr[:500] if stderr else "",
            "generated_code": (code or "")[:2000],
            "mitre_attack": data.get("mitre_attack", []),
            "investigation_metadata": data.get("investigation_metadata", {}),
            "plain_english_summary": data.get("plain_english_summary", ""),
            # v3 fields
            "execution_mode": data.get("execution_mode", ""),
            "path_d_fallback": data.get("path_d_fallback", False),
            "path_d_reason": data.get("path_d_reason", ""),
            "needs_human_review": data.get("needs_human_review"),
            "review_reason": data.get("review_reason", ""),
            "autonomy_level": data.get("autonomy_level", ""),
            "tools_executed": data.get("tools_executed", 0),
        }
        _update_task_status(
            conn, task_id, status, output,
            tokens_in=tokens_in, tokens_out=tokens_out,
            execution_ms=execution_ms, severity=severity,
            model_name=model_name,
            path_taken=path_taken, generated_code=generated_code,
        )

        # 2. Save investigation pattern (no LLM)
        if status == "completed":
            _save_pattern(
                conn, task_type, siem_event.get("rule_name", ""),
                code, iocs, findings, risk_score, True,
            )

        # 3. Create investigation row (skip embedding in FAST_FILL)
        if status == "completed" and not FAST_FILL:
            investigation_id = _create_investigation(
                conn, tenant_id, task_id, verdict,
                risk_score, confidence, memory_summary or stdout[:2000],
                model_name=model_name,
            )

        # 4. Audit: investigation_completed
        if tenant_id:
            _insert_audit_event(
                conn, tenant_id, "investigation_completed",
                "agent_task", task_id,
                {
                    "verdict": verdict, "risk_score": risk_score,
                    "status": status, "execution_ms": execution_ms,
                    "investigation_id": investigation_id,
                    "ioc_count": len(iocs), "finding_count": len(findings),
                },
                trace_id=trace_id,
            )

        # NOTIFY for SSE real-time updates (Mission 9)
        if status == "completed" and tenant_id:
            try:
                with _savepoint(conn, "sp_notify_task_completed"):
                    with conn.cursor() as cur:
                        notify_payload = json.dumps({
                            "task_id": task_id,
                            "tenant_id": tenant_id,
                            "verdict": verdict,
                            "risk_score": risk_score,
                            "task_type": task_type,
                        })
                        cur.execute("NOTIFY task_completed, %s", (notify_payload,))
            except Exception as notify_err:
                print(f"NOTIFY failed (non-fatal): {notify_err}")

        # Update Redis dedup entry with investigation results (v2 dedup)
        _update_dedup_entry(conn, task_id, verdict, risk_score, status, siem_event)

        conn.commit()

        try:
            from data_plane.emit import emit_after_investigation_stored

            await emit_after_investigation_stored(
                task_id=task_id,
                tenant_id=tenant_id,
                verdict=verdict,
                risk_score=risk_score,
                task_type=task_type,
                trace_id=trace_id,
                investigation_id=investigation_id,
                status=status,
            )
        except Exception as dp_err:
            print(f"[DATA_PLANE] emit failed (non-fatal): {dp_err}")
      except Exception as e:
        conn.rollback()
        print(f"Store failed: {e}")
        status = "failed"
    # _db_conn context manager returns the connection to the pool on exit.

    result = StoreOutput(
        task_id=task_id,
        status=status,
        investigation_id=investigation_id,
        memory_saved=status == "completed",
        pattern_saved=status == "completed",
    )

    try:
        trace_store_apply_outcome(_span, verdict, risk_score, len(iocs) if isinstance(iocs, list) else 0, trace_id)
    except Exception:
        pass

    try:
        from metrics import record_investigation_completed
        record_investigation_completed(str(verdict or ""), str(status or ""))
    except Exception:
        pass

    return asdict(result)
