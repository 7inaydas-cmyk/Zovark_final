"""Registration helper for V2 pipeline stages."""


def get_v2_activities():
    """Return all V2/V3 stage activities for Temporal worker registration."""
    from .ingest import ingest_alert
    from .analyze import analyze_alert
    from .execute import execute_investigation
    from .assess import assess_results
    from .govern import apply_governance
    from .store import store_investigation
    return [ingest_alert, analyze_alert, execute_investigation,
            assess_results, apply_governance, store_investigation]


def get_v2_workflows():
    """Return all V2 workflows for Temporal worker registration.

    The class symbol is `InvestigationWorkflow` but the Temporal wire name
    remains `"InvestigationWorkflowV2"` — see
    worker/stages/investigation_workflow.py for the `@workflow.defn(name=...)`
    decorator that preserves the wire name.
    """
    from .investigation_workflow import InvestigationWorkflow
    return [InvestigationWorkflow]
