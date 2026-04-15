# SUPERSEDED

This change was never applied. It has been superseded by
[`telemetry-audit-fix`](../../telemetry-audit-fix/), which:

1. Folds in every task from this change's `tasks.md` (container-name discovery,
   telemetry probe, socket-proxy allowlist fix, healer start_period bump,
   `--debug` flag).
2. Adds four new findings from a fresh audit:
   - `check_signoz` warmup flow so cold stacks don't report false `degraded`
   - `--prime` flag on `stack_healthcheck.sh` for one-command cold verification
   - `docs/TELEMETRY_ARCHITECTURE.md` committed reference with file:line anchors
   - CLAUDE.md correction for the stale manual-schema-migrator instruction

The file tree is preserved here for audit / history purposes. Do NOT apply
this change — apply `telemetry-audit-fix` instead.
