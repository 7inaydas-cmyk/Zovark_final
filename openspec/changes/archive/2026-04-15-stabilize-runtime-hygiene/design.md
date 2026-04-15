## Context

This change is the direct output of the Phase 2 (**FAIL**) + Phase 3 (**PASS WITH WARNINGS**) findings from the pre-YC audit conducted on 2026-04-14. The audit verified that:

1. The 7 recently-landed audit fixes (Phase 4) are all wired end-to-end.
2. The 3 "open" calibration gaps (Phase 5) are already fixed on this branch (`output_validator.py:52-57`, `detection.py:87` risk=80, `detection.py:853` risk=65, `assess.py:631` floor=25).
3. The real remaining pre-YC blockers are runtime hygiene: hardcoded credential fallbacks, dead code, a misleading class name, and a fragile import.

The stack is Go/Gin API + Python Temporal worker + Valkey + PostgreSQL + Redpanda (not NATS — that was a confusing doc claim; `worker/nats_consumer.py` is a deprecated stub, `worker/redpanda_consumer.py` is the active consumer imported at `worker/main.py:87`). This change does not alter the bus.

**Constraints:**

- **No downtime on Temporal rename.** Temporal routes workflow-start requests by workflow type name (by default, the Python class name). Renaming the class naively will break every in-flight workflow and every caller that still passes `"InvestigationWorkflowV2"` as a string. We MUST preserve the wire name via `@workflow.defn(name="InvestigationWorkflowV2")`.
- **No CI red on merge.** CI workflows at `.github/workflows/ci.yml` and `coverage.yml` currently rely on the `:-default` fallbacks in `docker-compose.yml` to boot ephemeral postgres and redis. Removing fallbacks and not updating CI in the same commit will red-out main. The fix must land atomically: fallback removal + CI `env:` blocks in one PR.
- **No air-gap regression.** `docker-compose.airgap.yml` is used by customers in offline installs. Replacing literal passwords with `${VAR}` references is correct, but operators MUST get a clear error message at boot if the variable is unset — not a silent failure.
- **Pydantic Settings is the anchor.** `worker/settings.py` already uses `pydantic-settings` with `SecretStr` and `env_prefix="ZOVARK_"`. Removing the `_DEFAULT_*` class constants converts the fields to implicitly-required (Pydantic raises `ValidationError` on missing env, which Temporal worker boot already catches and logs cleanly).
- **No speculative refactor.** Don't restructure `_legacy_activities.py` beyond inlining `decrement_active`. Don't rename files beyond `InvestigationWorkflowV2`. Don't touch anything Phase 6 or Phase 7 flagged — those are different changes.

## Goals / Non-Goals

**Goals:**

- After merge, `grep -r 'hydra_dev_2026\|hydra-redis-dev-2026\|sk-zovark-dev-2026' docker-compose*.yml helm/ k8s/ worker/*.py worker/**/*.py` returns **zero** hits.
- After merge, `env -i PATH=$PATH docker compose up -d` fails with a readable error identifying the first missing env var.
- After merge, a populated `.env` produces the same runtime behavior as today: `bash autoresearch/cycle10/verify_all.sh` returns 15/15.
- After merge, `worker/nats_consumer.py` does not exist.
- After merge, `grep -R "InvestigationWorkflowV2" worker/` shows the old string only in `redpanda_consumer.py` (wire-name default) and in `@workflow.defn(name="InvestigationWorkflowV2")` — the Python class itself is named `InvestigationWorkflow`.
- After merge, `rg 'ZOVARK_LLM_KEY' worker/stages/ worker/finetuning/` shows the env var referenced via `settings.llm_key` only; no hardcoded fallback literals remain.
- After merge, `.env.example` is a complete, placeholder-only template that boots the stack only after every value is replaced.

**Non-Goals:**

- Migration renumbering (041/050/051/052 duplicates) — out of scope, separate change.
- README, LICENSE, CHANGELOG — out of scope, separate change.
- Demo scaffolding — out of scope.
- Agent healer `hydra-mvp-*` container rename — out of scope (audit M13, follow-up change).
- Moving CI creds to GitHub Secrets — out of scope (founder decision §7).
- Rotating the physical `.env` on operator disk — out of scope (operator work, not code).
- Replacing `keep_alive: 30m` hardcoded llama.cpp-ism in `llm_gateway.py` — out of scope (audit M16, cosmetic).
- Any pipeline or detection logic changes.

## Decisions

### Decision 1 — Temporal workflow rename preserves the wire name

**What:** rename Python class `InvestigationWorkflowV2` → `InvestigationWorkflow`, but annotate with `@workflow.defn(name="InvestigationWorkflowV2")` so the Temporal registration string is unchanged.

**Why:** option (b) "register both as aliases" doubles the registration machinery and is harder to clean up later; option (c) "drain before rename" forces a downtime window. Option (a), explicit `name=` parameter, is supported by the Temporal Python SDK natively, requires zero coordination, and leaves a single clean follow-up (change the default string in `redpanda_consumer.py:146` plus the `name=` parameter in a future change, once every running workflow has completed).

**Alternatives considered:**
- Rename class AND wire name simultaneously. Rejected — breaks in-flight executions.
- Register both class names with a shim. Rejected — doubles Temporal metadata, harder rollback.
- Keep Python class name as-is and only update comments. Rejected — the whole point is to remove the misleading name.

### Decision 2 — Fail-fast via Pydantic, not via compose-level validation

**What:** rely on Pydantic Settings to raise `ValidationError` when required env vars are missing at worker startup, rather than adding custom validation scripts or compose-level preflight.

**Why:** pydantic-settings already raises clean, readable errors naming the missing field. Adding a second layer (e.g., a `preflight.sh`) would duplicate the contract and create a second place to get out of sync. Compose v2 already fails on missing `${VAR}` substitutions with a readable "The POSTGRES_PASSWORD variable is not set" error when no default is provided — that covers the non-Python processes (postgres, redis, temporal-auto-setup, etc.). Python processes go through Pydantic. Two layers, each native to their runtime.

**Alternatives considered:**
- Custom `scripts/check_env.sh` preflight. Rejected — adds maintenance burden for a thing the runtime already does correctly.
- Default-value validators with explicit `raise` in settings.py. Rejected — Pydantic does this for free.

### Decision 3 — Helm strategy: empty strings + `required` helper

**What:** in `helm/zovarc/values.yaml`, replace secret literals with empty strings (`dbPassword: ""`, `litellmMasterKey: ""`, `jwtSecret: ""`). In the corresponding templates (e.g., `templates/deployment.yaml`), wrap references with Helm's `required` helper: `{{ required "dbPassword is required — pass via --set or --values" .Values.dbPassword }}`.

**Why:** leaving the keys documented (but empty) in `values.yaml` is better for discoverability than removing them — operators can still see the full set of configurables in one file. The `required` helper gives a clear error at render time (`helm install` or `helm template`) rather than silently rendering `""` into a Secret. This matches Helm best practice and is idempotent.

**Alternatives considered:**
- Remove the keys entirely. Rejected — operators then have to read the templates to find the configurables.
- Leave keys with sentinel strings like `"CHANGE_ME"` that the chart rejects in a helper. Rejected — more code, same outcome as `required`.

### Decision 4 — `k8s/base/worker/deployment.yaml` Redis DSN via `secretKeyRef`

**What:** replace the literal `value: "redis://:hydra-redis-dev-2026@redis:6379/0"` with:

```yaml
- name: REDIS_URL
  valueFrom:
    secretKeyRef:
      name: zovark-worker-secrets
      key: redis-url
```

Document in `k8s/base/secrets.yaml.example` a Secret named `zovark-worker-secrets` with the `redis-url` key populated at install time (via kustomize `secretGenerator`, Sealed Secrets, or `kubectl create secret`).

**Why:** consistent with how K8s secrets are supposed to flow. Uses an existing pattern (`secrets.yaml.example` already exists per audit). Zero runtime overhead. Removes the `# FIX #13` annotation.

**Alternatives considered:**
- `envFrom: secretRef:` at the pod level. Rejected — pulls every key in the secret as env vars, can leak other values into the pod env. `secretKeyRef` is narrower.
- ConfigMap instead of Secret. Rejected — DSN contains a password.

### Decision 5 — Centralize LLM key loading on `settings.llm_key`

**What:** single source of truth is the `llm_key: SecretStr` field already declared in `worker/settings.py`. Three existing call sites become:

```python
from settings import settings
ZOVARK_LLM_KEY = settings.llm_key.get_secret_value()
```

or, for modules that import settings lazily:

```python
from settings import settings
# … later:
"Authorization": f"Bearer {settings.llm_key.get_secret_value()}"
```

Pydantic raises `ValidationError` on missing env; no fallback literals anywhere.

**Why:** rotating the LLM key today requires editing three files. After this change, it's `export ZOVARK_LLM_KEY=...`. Removes 3 hits of `sk-zovark-dev-2026` from the grep.

**Alternatives considered:**
- Keep per-module env reads but remove fallbacks. Rejected — still three places to audit; Pydantic raises are more consistent.

### Decision 6 — Orphan-file deletion gated on a final string-based import sweep

**What:** before deleting `worker/context_manager.py`, `worker/investigation_cache.py`, and `worker/rate_limiter.py`, run:

```
rg -t py "context_manager|investigation_cache|rate_limiter" worker/ api/ autoresearch/ sdk/ cmd/ scripts/ tests/
```

For each match, verify it's not a real importer (string-based `importlib.import_module`, plugin registry, config-driven loader). `worker/nats_consumer.py` is deleted unconditionally — it's a 12-line stub and even the docstring says "deprecated." `worker/redis_client.py` is deleted AFTER inlining `decrement_active` into `_legacy_activities.py`.

**Why:** the Phase 3 agent's orphan analysis was based on direct `import` statements, which misses `importlib`, string-based plugin registries, and config-driven lookups. A final sweep is cheap insurance. If any match is found, the file is kept and documented as a follow-up.

**Alternatives considered:**
- Delete unconditionally based on the Phase 3 report. Rejected — small risk of a silent runtime break.
- Park all orphan deletions for a separate change. Rejected — the whole point of this change is cleanup; splitting further adds PR overhead.

### Decision 8 — `worker/activities/__init__.py` keeps its bare import (Option A, 2026-04-15)

**What:** Do NOT change `from _legacy_activities import …` to a relative import. Add a comment block explaining why the bare import is structurally necessary and instructing future readers not to "fix" it.

**Why:** Post-planning verification in the running worker container proved the audit's prescribed fix was wrong. `_legacy_activities.py` sits at `worker/_legacy_activities.py` (sibling of `worker/activities/`), not inside `worker/activities/`. A relative `from ._legacy_activities import …` inside `worker/activities/__init__.py` resolves to `worker/activities/_legacy_activities.py`, which does not exist. Verified with `importlib.import_module('._legacy_activities', package='activities')` → `ModuleNotFoundError`. The bare import works because `/app` is on `sys.path` at container start, so `_legacy_activities` resolves as a top-level module.

**Alternatives considered:**
- **Option B — Make `worker/` a proper package** (add `worker/__init__.py`, rewrite every import to use `worker.` prefix): ~100+ import sites, breaks `worker/main.py`, all of `worker/stages/*`, tests, Temporal registration. Multi-hour refactor out of scope for this change.
- **Option C — Move `_legacy_activities.py` into `worker/activities/`**: the file is 1200+ lines with intra-module state, and is named `_legacy_` to signal pending deprecation. Moving it churns imports across the codebase.
- **Option A (chosen) — Leave bare import, add explanatory comment.** Zero code risk, still satisfies the audit's underlying concern (future readers won't be confused) via documentation instead of restructuring.

**Blast radius:** Zero. No behavior change.

### Decision 7 — CI workflow compatibility lands in the same change

**What:** same commit that removes compose fallbacks also adds explicit `env:` blocks to `.github/workflows/ci.yml` and `coverage.yml` with the existing test-credential values, annotated `# test-only: ephemeral CI postgres, not a real secret`. No move to GitHub Secrets in this change.

**Why:** CI is the canary for merge safety. If CI reds-out, rollback is a revert. Keeping test values inline with an explicit comment is a pragmatic stopgap; moving to GitHub Secrets is a hygiene improvement worth doing but out of scope here (and requires repo-admin action, not a code change).

**Alternatives considered:**
- Migrate CI creds to GitHub Secrets now. Rejected — requires founder action to create the secrets in the GitHub UI, which is out of the code-review path.

## Risks / Trade-offs

### Risk 1 — Temporal rename still breaks in-flight workflows

**Scenario:** `@workflow.defn(name=...)` is respected by Temporal, but if any other tooling (dashboards, admin CLI, `tctl` scripts) uses the old Python class name (`InvestigationWorkflowV2`) as a string match, it may misbehave.

**Mitigation:** grep for the string `InvestigationWorkflowV2` across the entire repo before merge; document every hit in `tasks.md §3.4`. Preserve the wire name. Run the regression script against a running stack with at least one workflow in-flight.

**Blast radius if hit:** one workflow fails to resume; analyst has to re-submit. No data loss, no cascade.

### Risk 2 — Compose fallback removal breaks customer air-gap installs

**Scenario:** a customer has been relying on `docker-compose.airgap.yml` defaults to boot their offline install. Removing fallbacks surfaces the missing env as a clear error, but the customer then has to update their operator runbook.

**Mitigation:** document the behavior change in the tasks.md output note; update `docker-compose.airgap.yml`'s top-of-file comment to point operators at `.env.example`; ensure the error message names the specific missing variable (compose v2 does this by default).

**Blast radius if hit:** one-time operator friction on the next air-gap install. Zero runtime risk to a running deployment.

### Risk 3 — Orphan deletion accidentally removes a live file

**Scenario:** one of `context_manager.py`, `investigation_cache.py`, `rate_limiter.py` turns out to be imported via `importlib` or a plugin registry that static analysis missed.

**Mitigation:** the string-sweep gate in Decision 6. If any sweep hits, the file is kept. The deletion list is provisional until the sweep runs.

**Blast radius if hit:** `ImportError` at worker startup. Immediately visible, easy to revert.

### Risk 4 — CI still relies on the compose fallbacks in ways not captured by `env:` blocks

**Scenario:** some CI step indirectly relies on the default via a subprocess that doesn't inherit the explicit env.

**Mitigation:** run the full CI workflow locally (via `act` or a branch push) before merging. Capture the first red line if it happens and fix in the same PR.

**Blast radius if hit:** CI red on merge. Revert is a one-line.

### Risk 5 — Pydantic `ValidationError` at startup is louder than the old silent default

**Scenario:** a fresh dev clone without a populated `.env` today boots a working stack and only later notices everything is pointing at `hydra_dev_2026`. After this change, they get a hard error immediately.

**Mitigation:** this is the intended behavior. Update `HANDOVER.md §3` to note that `.env` must be populated before `docker compose up -d`. (Documentation is not a separate OpenSpec change — `HANDOVER.md` edits ride with code.)

**Blast radius if hit:** one confused developer; fix is `cp .env.example .env && edit`.

### Trade-off — Choose readability over minimum diff

The Helm `required` helper adds a few template lines per secret. Alternative is to drop the keys and rely on operators reading templates. We chose readability: `values.yaml` stays the canonical list of configurables, and the error message at install time names the specific key.

### Trade-off — Single atomic change vs. split by file type

Could split this into (a) dead-code deletion, (b) secret hygiene, (c) rename + centralization. Chose single change to minimize PR review overhead and because verification is the same for all three (rebuild worker + run regression). Rollback is still one `git revert`.

## Migration Plan

Not applicable — no schema changes, no data migrations, no contract changes. In-flight workflows are preserved by the wire-name-preservation decision. On deploy:

1. Merge PR (all changes atomic).
2. Operator populates `.env` from the regenerated `.env.example` before `docker compose up -d`. If `.env` is already populated with real values, no action needed — the old defaults were never used in a properly-configured deployment.
3. `docker compose build worker && docker compose up -d`.
4. Run `bash autoresearch/cycle10/verify_all.sh` to confirm regression.
5. No backfill required.

## Open Questions

Escalated to founder decisions in §7 of the top-level reply. See `tasks.md §0` for the blocking-on-decision markers.
