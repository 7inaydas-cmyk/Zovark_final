## Why

The pre-YC audit (see conversation transcript dated 2026-04-14) surfaced a cluster of runtime hygiene problems that are not correctness bugs but are the exact signals a technical reviewer looks for in the first ten minutes of due diligence:

- **Hardcoded dev-credential fallbacks** in production-adjacent config. `docker-compose.yml`, `helm/zovarc/values.yaml`, `k8s/base/worker/deployment.yaml`, and several `worker/*.py` files all default to known strings (`hydra_dev_2026`, `hydra-redis-dev-2026`, `sk-zovark-dev-2026`, the weak `JWT_SECRET`). `grep -r hydra_dev_2026 .` returns ~14 hits across production-adjacent files. Audit calls this Phase 2 **FAIL**.
- **Dead files and a fragile import**. `worker/nats_consumer.py` is a 12-line stub that contradicts `HANDOVER.md §2.3` and the user-stated architecture. `worker/activities/__init__.py:3` uses a bare `from _legacy_activities import` that breaks if `worker/` is ever installed as a subpackage. Several zero-importer orphans (`redis_client.py`, `rate_limiter.py`, `context_manager.py`, `investigation_cache.py`) add noise.
- **Misleading class name**. `InvestigationWorkflowV2` in `worker/stages/investigation_workflow.py` is the v3 tool-calling path, not a v2 legacy. The naming confuses every new reader of the codebase.
- **Scattered LLM-key loading**. Three call sites (`worker/stages/assess.py:54`, `worker/stages/analyze.py:52`, `worker/finetuning/evaluator.py:18`) independently read `ZOVARK_LLM_KEY` with a fallback literal. Rotating the key requires changing three places.
- **Stale `.env.example`**. Doesn't cover all vars `settings.py` + `docker-compose.yml` + `helm/values.yaml` expect.

Five of the seven audit "known open gaps" turned out to already be fixed on this branch. The remaining runtime/hygiene debt is what this change closes. Migrations (Phase 6), README/LICENSE/CHANGELOG (Phase 7), demo, and branding cleanup are **out of scope** — they are separate changes.

## What Changes

### Dead-code removal
- **DELETE** `worker/nats_consumer.py` (12-line deprecated stub, zero importers, contradicts `HANDOVER.md`).
- **DELETE** `worker/redis_client.py` after inlining its one caller (`decrement_active` used by `worker/_legacy_activities.py`) into `_legacy_activities.py` itself.
- **DELETE** `worker/rate_limiter.py`, `worker/context_manager.py`, `worker/investigation_cache.py` — but only after a final string-based-import sweep (see tasks.md §1.4). If any sweep matches, park the file for a separate change and document here.

### Structural fixes
- **DOCUMENT** `worker/activities/__init__.py:3` — the bare `from _legacy_activities import (...)` is kept as-is (Option A, 2026-04-15 founder decision). The audit's prescribed relative-import "fix" was structurally wrong: `_legacy_activities.py` sits at `worker/_legacy_activities.py` (sibling of `worker/activities/`), and `worker/` is not a Python package. A comment block is added explaining why the bare import is correct so future readers do not revisit it. See `design.md` "Decision 8".
- **RENAME** Python class `InvestigationWorkflowV2` → `InvestigationWorkflow` in `worker/stages/investigation_workflow.py`. Wire name SHALL be preserved via `@workflow.defn(name="InvestigationWorkflowV2")` so in-flight workflows and operators using the old string continue to resolve. Update all Python importers in `worker/stages/register.py`, `worker/main.py`, and anywhere else that references the class symbol. Do NOT change `worker/redpanda_consumer.py:146-148` default string — the wire name stays.

### Secret hygiene — remove default fallbacks
- **EDIT** `docker-compose.yml` — remove `:-literal` fallbacks at lines 18, 54, 63, 95, 372, 441, 803, 805. Every `${VAR}` becomes required; compose v2 will fail with a clear error if unset.
- **EDIT** `docker-compose.optional.yml:46` — remove NATS password fallback.
- **EDIT** `docker-compose.airgap.yml` — lines 14, 49, 98, 123, 142 — replace literal passwords with `${VAR}` references and document the overlay as operator-managed.
- **EDIT** `helm/zovarc/values.yaml` — lines 81, 107, 134-136. Strategy: keep keys present with empty-string values, add a `required` helper in the corresponding templates so `helm install` without overrides fails fast with a readable message. No secret strings remain in the chart.
- **EDIT** `k8s/base/worker/deployment.yaml:51` — replace literal `redis://:hydra-redis-dev-2026@redis:6379/0` with `valueFrom.secretKeyRef` pointing at a new key `redis-url` in an existing or new `zovark-worker-secrets` Secret. Update `k8s/base/secrets.yaml.example` to show the expected Secret shape. Remove the `# FIX #13` annotation since this IS fix #13.
- **EDIT** `worker/settings.py` — delete `_DEFAULT_DB_PASSWORD`, `_DEFAULT_REDIS_PASSWORD`, and the `SecretStr("hydra_dev_2026")` / `SecretStr("hydra-redis-dev-2026")` literal defaults at lines 20-21, 40, 46, 150-151. Fields become required; Pydantic raises `ValidationError` at startup if env is missing, which is the correct fail-fast behavior.

### Centralized LLM key loading
- **EDIT** `worker/stages/assess.py:54`, `worker/stages/analyze.py:52`, `worker/finetuning/evaluator.py:18` — each call site switches from `os.environ.get("ZOVARK_LLM_KEY", "sk-zovark-dev-2026")` to `from settings import settings; settings.llm_key.get_secret_value()`. Remove the literal fallback entirely.
- The `settings.llm_key: SecretStr` field in `worker/settings.py` becomes the single source of truth.

### `.env.example` regeneration
- **REGENERATE** `.env.example` as the union of every env var consumed by `settings.py`, `docker-compose.yml`, `helm/values.yaml`, and `k8s/base/*`. Every value is a placeholder that explicitly does NOT work if pasted unchanged (e.g. `POSTGRES_PASSWORD=REPLACE_ME_openssl_rand_base64_32`). Sections organized by subsystem (DB, Cache, LLM, Auth, Observability, etc.). Header block documents how to generate each kind of secret.

### CI workflow compatibility
- **EDIT** `.github/workflows/ci.yml` and `.github/workflows/coverage.yml` — add explicit `env:` blocks at the job or step level with test-only credential values (same strings as today), annotated `# test-only: ephemeral CI postgres, not a real secret`. This keeps CI green the same commit as fallback removal. No GitHub Secrets migration in this change (see founder decisions).

### Verification steps (required before merge)
1. **Fail-fast on missing env**: `env -i PATH=$PATH docker compose up -d` MUST exit non-zero with a readable error naming the missing variable (e.g. "POSTGRES_PASSWORD is required").
2. **Happy-path startup**: with a populated `.env`, `docker compose up -d` MUST bring all core services to `healthy` within 120 seconds.
3. **Worker rebuild**: `docker compose build worker && docker compose up -d worker` MUST succeed without errors.
4. **Regression suite**: `bash autoresearch/cycle10/verify_all.sh` MUST return 15/15 (10 attacks ≥65 risk, 5 benign ≤25 risk).
5. **Dedup regression**: `bash autoresearch/cycle10/dedup_stress_test.sh` MUST return 13-14/14.
6. **In-flight workflow compatibility**: after rename, submitting a task that starts `InvestigationWorkflowV2` via the legacy wire name MUST still work (proves `@workflow.defn(name=...)` preserved the registration).

## Capabilities

### New Capabilities

- `runtime-config-hygiene`: defines the repository-wide rule that no production-adjacent config file (compose, helm, k8s, Python settings) SHALL contain hardcoded credential fallback defaults, and that all LLM key loading SHALL flow through a single centralized accessor. Specifies the required `.env.example` shape and the fail-fast startup contract.

### Modified Capabilities

- None. (`schema-migration-integrity` is the only existing baseline capability and is not touched by this change.)

## Impact

- **Affected code** — `worker/nats_consumer.py`, `worker/redis_client.py`, `worker/rate_limiter.py`, `worker/context_manager.py`, `worker/investigation_cache.py`, `worker/_legacy_activities.py`, `worker/activities/__init__.py`, `worker/stages/investigation_workflow.py`, `worker/stages/register.py`, `worker/stages/assess.py`, `worker/stages/analyze.py`, `worker/finetuning/evaluator.py`, `worker/settings.py`, `worker/main.py`, `docker-compose.yml`, `docker-compose.optional.yml`, `docker-compose.airgap.yml`, `helm/zovarc/values.yaml`, `helm/zovarc/templates/*` (touched only to add `required` helpers), `k8s/base/worker/deployment.yaml`, `k8s/base/secrets.yaml.example`, `.env.example`, `.github/workflows/ci.yml`, `.github/workflows/coverage.yml`.
- **Out of scope (explicit)** — no migration changes (Phase 6 findings); no `README.md`, `LICENSE`, or `CHANGELOG.md` work (Phase 7); no `demo/` scaffolding; no `agent/healer.py` `hydra-mvp-*` container rename (follow-up change); no CI-secrets migration to GitHub Secrets (follow-up change if founder approves).
- **Runtime behavior** — no functional change to the pipeline. No new endpoints, no tool catalog changes, no verdict logic changes. This is purely configuration and code-shape cleanup. Risk of runtime regression is limited to the Temporal rename (mitigated by preserving the wire name) and the CI workflow update (mitigated by explicit `env:` blocks).
- **Operator impact** — operators who boot Zovark without a populated `.env` today get a working system with known-default credentials. After this change, they get a clear error naming the missing variable. This is the intended behavior change. Document it in the `.env.example` header and in the tasks output note.
- **Customer impact** — any customer using the Helm chart with default values MUST now pass `--set` or `--values` with real secrets. Document in `helm/zovarc/README.md` (already exists).
- **Downstream changes unblocked** — resolves audit findings M1, M2, M3 (secrets), M8, M9 (LLM key centralization), M11 (bare import), M12 (nats_consumer), M15 (rename), M17 (orphan cleanup). Does NOT resolve M3's migration renumbering, M10's init.sql fresh-install gap, M4's migration 068 coordination, or any Phase 7 item.
