"""
Stage 4.5: GOVERN — Apply autonomy level to determine if human review is needed.
NO LLM calls. DB query for governance config only.

Autonomy levels:
  observe:    All investigations need human review
  assist:     Only non-benign need review
  autonomous: Only edge cases (inconclusive, error) need review
"""
import os

import psycopg2
from psycopg2.extras import RealDictCursor
from temporalio import activity

# stabilize-runtime-hygiene: centralized DB URL via settings.
from settings import settings as _settings
DATABASE_URL = os.environ.get("DATABASE_URL", _settings.database_url)


def _get_db():
    return psycopg2.connect(DATABASE_URL)


def _get_governance_config(tenant_id: str, task_type: str) -> dict:
    """Query governance_config for this tenant + task_type."""
    default = {"autonomy_level": "observe", "consecutive_correct": 0, "upgrade_threshold": 20}
    try:
        conn = _get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Try specific task_type first, then wildcard
                cur.execute(
                    "SELECT autonomy_level, consecutive_correct, upgrade_threshold "
                    "FROM governance_config "
                    "WHERE tenant_id = %s AND (task_type = %s OR task_type = '*') "
                    "ORDER BY CASE WHEN task_type = %s THEN 0 ELSE 1 END "
                    "LIMIT 1",
                    (tenant_id, task_type, task_type)
                )
                row = cur.fetchone()
                if row:
                    return dict(row)
        finally:
            conn.close()
    except Exception as e:
        activity.logger.warning(f"Failed to load governance config: {e}")
    return default


@activity.defn
async def apply_governance(data: dict) -> dict:
    """Apply autonomy level to determine if human review is needed.

    Input: assess output dict + tenant_id + task_type
    Returns: same dict with needs_human_review and review_reason added
    """
    # OTEL span — audit 2.17: guarantee .end() runs on every exit path via a
    # refcount-bound guard (same pattern as assess.py).
    class _SpanGuard:
        __slots__ = ("span", "done")

        def __init__(self, s):
            self.span = s
            self.done = False

        def finish(self):
            if self.span is not None and not self.done:
                self.done = True
                try:
                    self.span.end()
                except Exception:
                    pass

        def __del__(self):
            self.finish()

    try:
        from tracing import get_tracer
        _span = get_tracer().start_span("stage.govern")
        _span.set_attribute("zovark.task_type", data.get("task_type", ""))
    except Exception:
        _span = None
    _span_guard = _SpanGuard(_span)  # noqa: F841

    tenant_id = data.get("tenant_id", "")
    task_type = data.get("task_type", "")
    verdict = data.get("verdict", "inconclusive")

    config = _get_governance_config(tenant_id, task_type)
    autonomy = config.get("autonomy_level", "observe")

    if autonomy == "observe":
        data["needs_human_review"] = True
        data["review_reason"] = "Observe mode: all investigations require analyst review"
    elif autonomy == "assist":
        if verdict != "benign":
            data["needs_human_review"] = True
            data["review_reason"] = f"Assist mode: {verdict} requires analyst review"
        else:
            data["needs_human_review"] = False
            data["review_reason"] = ""
    elif autonomy == "autonomous":
        if verdict in ("inconclusive", "needs_review", "needs_manual_review", "needs_analyst_review", "error"):
            data["needs_human_review"] = True
            data["review_reason"] = f"Autonomous mode: {verdict} requires analyst review"
        else:
            data["needs_human_review"] = False
            data["review_reason"] = ""
    else:
        # Unknown autonomy level — default to observe
        data["needs_human_review"] = True
        data["review_reason"] = f"Unknown autonomy level '{autonomy}', defaulting to observe"

    data["autonomy_level"] = autonomy

    if _span:
        try:
            _span.set_attribute("governance.autonomy_level", autonomy)
            _span.set_attribute("governance.needs_review", data.get("needs_human_review", True))
        except Exception:
            pass
    _span_guard.finish()

    return data
