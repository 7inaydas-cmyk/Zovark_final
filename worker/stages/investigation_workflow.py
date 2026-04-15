"""
Investigation Workflow V2/V3 — 6-stage pipeline with OpenTelemetry tracing.

Stages:
  1. INGEST  (30s)  — dedup, PII mask, skill retrieval
  2. ANALYZE (5min) — plan loading or LLM tool selection
  3. EXECUTE (2min) — tool runner (v3) or Docker sandbox (v2)
  4. ASSESS  (1min) — verdict, summary, FP analysis
  4.5 GOVERN (10s)  — autonomy check
  5. STORE   (30s)  — DB writes, memory, patterns
"""
from datetime import timedelta
from temporalio import workflow  # noqa: F401 — re-exported by stages/__init__

with workflow.unsafe.imports_passed_through():
    from stages.ingest import ingest_alert
    from stages.analyze import analyze_alert
    from stages.execute import execute_investigation
    from stages.assess import assess_results
    from stages.govern import apply_governance
    from stages.store import store_investigation


# Wire-name preservation: the Temporal workflow type string remains
# "InvestigationWorkflowV2" for backwards compatibility with in-flight workflows
# and every caller that passes the string as `ZOVARK_WORKFLOW_VERSION`. The
# Python class symbol is `InvestigationWorkflow` (the "V2" suffix was a
# historical naming artifact — this IS the current investigation pipeline).
# See openspec/changes/stabilize-runtime-hygiene/design.md Decision 1.
@workflow.defn(name="InvestigationWorkflowV2")
class InvestigationWorkflow:
    """6-stage investigation pipeline with tracing."""

    @workflow.run
    async def run(self, task_data: dict) -> dict:
        # Ticket 7: Root span `investigation.pipeline` runs inside ingest_alert (replay-safe).
        # Temporal TracingInterceptor links analyze/execute/assess/store activities to the same trace.
        # Extract task_id from Temporal workflow ID (same as legacy)
        info = workflow.info()
        task_id = info.workflow_id.replace("task-", "")

        # Fetch full task from DB via legacy fetch_task activity
        # Timeout allows for retry loop (8 retries × ~1-3s each = up to 15s)
        full_task = await workflow.execute_activity(
            "fetch_task", task_id,
            start_to_close_timeout=timedelta(seconds=30),
        )
        if not full_task:
            return {"status": "failed", "task_id": task_id, "error": "Task not found"}

        # Inject task_id and tenant_id into task_data
        full_task["task_id"] = task_id

        # Stage 1: INGEST — dedup, PII mask, skill match (no LLM)
        ingested = await workflow.execute_activity(
            ingest_alert, full_task,
            start_to_close_timeout=timedelta(seconds=30),
        )

        if ingested.get("is_duplicate"):
            return {
                "status": "deduplicated",
                "task_id": ingested["task_id"],
                "duplicate_of": ingested.get("duplicate_of"),
            }

        # Stage 2: ANALYZE — generate investigation code/plan (LLM boundary)
        # 900s timeout: Path C (full LLM gen) takes 120-270s per call on RTX 3050,
        # and the LLM queues requests sequentially on single GPU
        analyzed = await workflow.execute_activity(
            analyze_alert, ingested,
            start_to_close_timeout=timedelta(seconds=900),
        )

        # Determine execution mode
        execution_mode = analyzed.get("execution_mode", "sandbox")
        path_taken = analyzed.get("path_taken", analyzed.get("source", "unknown"))

        if execution_mode == "tools":
            # V3: tool-calling path
            plan = analyzed.get("plan", [])
            if not plan:
                # No plan available — fail gracefully
                return {"status": "failed", "task_id": ingested["task_id"],
                        "error": f"No investigation plan available (path={path_taken})",
                        "verdict": "needs_manual_review", "risk_score": 0}

            # Stage 3: EXECUTE — in-process tool runner (no Docker)
            executed = await workflow.execute_activity(
                execute_investigation,
                {
                    "plan": plan,
                    "siem_event": ingested.get("siem_event", {}),
                    "task_type": ingested["task_type"],
                    "tenant_id": ingested["tenant_id"],
                    "execution_mode": "tools",
                },
                start_to_close_timeout=timedelta(seconds=60),
            )
        else:
            # V2: sandbox path
            if not analyzed.get("code"):
                return {"status": "failed", "task_id": ingested["task_id"],
                        "error": "Code generation produced no output"}

            # Stage 3: EXECUTE — Docker sandbox (v2)
            executed = await workflow.execute_activity(
                execute_investigation,
                {"code": analyzed["code"], "task_type": ingested["task_type"],
                 "source": analyzed.get("source", ""), "execution_mode": "sandbox"},
                start_to_close_timeout=timedelta(seconds=120),
            )

        # Stage 4: ASSESS — verdict + summary (LLM boundary)
        assessed = await workflow.execute_activity(
            assess_results,
            {
                **executed,
                "task_id": ingested["task_id"],
                "tenant_id": ingested["tenant_id"],
                "task_type": ingested["task_type"],
                "siem_event": ingested.get("siem_event", {}),
                "path_taken": path_taken,
                "execution_mode": execution_mode,
            },
            start_to_close_timeout=timedelta(seconds=60),
        )

        # Stage 4.5: GOVERN — autonomy check (no LLM)
        governed = await workflow.execute_activity(
            apply_governance,
            {
                **assessed,
                "tenant_id": ingested["tenant_id"],
                "task_type": ingested["task_type"],
            },
            start_to_close_timeout=timedelta(seconds=10),
        )

        # Extract trace_id from task row (set by Go API at INSERT)
        trace_id = full_task.get("trace_id", "")

        # Stage 5: STORE — persist everything (no LLM)
        # governed values (verdict, risk_score, recommendations) must take
        # precedence over raw executed values — spread governed LAST
        stored = await workflow.execute_activity(
            store_investigation,
            {
                **executed,
                **governed,
                "task_id": ingested["task_id"],
                "tenant_id": ingested["tenant_id"],
                "task_type": ingested["task_type"],
                "siem_event": ingested.get("siem_event", {}),
                "code": analyzed.get("code", ""),
                "tokens_in": analyzed.get("tokens_in", 0),
                "tokens_out": analyzed.get("tokens_out", 0),
                "path_taken": path_taken,
                "generated_code": analyzed.get("code", ""),
                "trace_id": trace_id,
                "execution_mode": execution_mode,
                "plan_executed": analyzed.get("plan", []),
            },
            start_to_close_timeout=timedelta(seconds=30),
        )

        return stored
