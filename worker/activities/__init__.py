# Shared activities from legacy module (used by non-investigation workflows).
# Investigation-specific activities are in worker/stages/*.py (V2 pipeline).
#
# Import note: the bare `from _legacy_activities import …` is intentional.
# `worker/` is not a Python package (no `worker/__init__.py`) — it is a
# directory added to `sys.path` at container start, and `_legacy_activities.py`
# sits at `/app/_legacy_activities.py` as a top-level module. A relative
# `from ._legacy_activities import …` inside this file would resolve to
# `worker/activities/_legacy_activities.py`, which does not exist. Do not
# "fix" this to a relative import without first making `worker/` a proper
# package — see openspec/changes/stabilize-runtime-hygiene/design.md.
from _legacy_activities import (  # noqa: F401
    fetch_task, update_task_status,
    log_audit, log_audit_event, record_usage,
    check_requires_approval, create_approval_request, update_approval_request,
    check_rate_limit_activity, decrement_active_activity, heartbeat_lease_activity,
    get_db_connection,
)
