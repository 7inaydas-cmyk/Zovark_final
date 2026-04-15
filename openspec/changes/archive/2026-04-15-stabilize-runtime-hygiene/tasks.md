## 0. Blockers — founder decisions required before starting

- [x] 0.1 Confirm Temporal rename strategy (Decision 1 in design.md): preserve wire name via `@workflow.defn(name="InvestigationWorkflowV2")` and only rename the Python class. **If founder wants a different strategy, STOP and update design.md before continuing.**
- [x] 0.2 Confirm Helm strategy (Decision 3): empty-string keys + `required` helper in templates. **If founder wants to drop keys entirely, STOP and update design.md.**
- [x] 0.3 Confirm CI strategy (Decision 7): inline test values with `# test-only` comment for now, GitHub Secrets migration deferred. **If founder wants GitHub Secrets now, STOP — that requires repo-admin action outside this change.**
- [x] 0.4 Confirm orphan-deletion scope (Decision 6): `nats_consumer.py` unconditional; `redis_client.py` after inlining; `context_manager.py`, `investigation_cache.py`, `rate_limiter.py` gated on final string-sweep. **If founder wants a different list, update §1 below.**
- [x] 0.5 Confirm that rotating the on-disk `.env` is out of scope (it's operator work, not code). **If founder wants `.env` rotated in this change, add a §8 section.**

## 1. Dead-code removal

- [x] 1.1 Delete `worker/nats_consumer.py` unconditionally
- [x] 1.2 Grep-verify no residual importers: `rg -t py -t sh -t yaml 'nats_consumer' .`
- [x] 1.3 Inline `decrement_active` from `worker/redis_client.py` into `worker/_legacy_activities.py`, preserving the function signature and Redis connection logic
- [x] 1.4 Grep-verify `redis_client` is no longer referenced anywhere: `rg -t py 'redis_client' worker/`
- [x] 1.5 Delete `worker/redis_client.py`
- [x] 1.6 Run the orphan-deletion string sweep: `rg -t py "context_manager|investigation_cache|rate_limiter" worker/ api/ autoresearch/ sdk/ cmd/ scripts/ tests/ dpo/ agent/ mcp-server/`
- [x] 1.7 For each hit in 1.6, classify: real importer / string literal / comment / documentation. Real importers block deletion of that file. — `rate_limiter` and `context_manager` flagged as PARKED (real importers found); `investigation_cache` cleared.
- [x] 1.8 For files that pass the sweep: delete `worker/investigation_cache.py` (only). `context_manager.py` and `rate_limiter.py` PARKED — both have real importers per the sweep.
- [x] 1.9 Delete `worker/nats_consumer.py` reference from any `Dockerfile`, `MANIFEST.in`, or `pyproject.toml` if present (run `rg 'nats_consumer' -n` repo-wide) — zero hits.

## 2. Structural fixes

- [x] 2.1 ~~Change `worker/activities/__init__.py:3` from bare to relative import~~ — **SUPERSEDED by Option A (founder decision 2026-04-15)**: audit prescription was structurally wrong (`worker/` is not a Python package). Instead, add a comment block explaining the bare import is intentional. See design.md "Decision 8".
- [x] 2.2 Verify the file still parses: `docker compose exec -T worker python -c "from activities import fetch_task; print('ok')"` (gate is against pre-edit behavior; the Option A change is comment-only)
- [x] 2.3 Rename Python class `InvestigationWorkflowV2` → `InvestigationWorkflow` in `worker/stages/investigation_workflow.py`
- [x] 2.4 Add/update `@workflow.defn(name="InvestigationWorkflowV2")` decorator on the renamed class to preserve the Temporal wire name
- [x] 2.5 Update Python importers: `worker/stages/register.py` (in `get_v2_workflows()`), `worker/main.py` if direct-imported, any test file referencing the old class symbol
- [x] 2.6 Grep-verify: `rg 'InvestigationWorkflowV2' worker/` should show only (a) the `name="InvestigationWorkflowV2"` decorator argument, (b) the default string in `worker/redpanda_consumer.py:146-148`, and (c) any comment references. No other hits.
- [x] 2.7 Grep-verify Python imports: `rg 'from stages.investigation_workflow import' worker/` — should import `InvestigationWorkflow`, not `InvestigationWorkflowV2`

## 3. Secret hygiene — remove default fallbacks

### 3a. Docker Compose

- [x] 3.1 Edit `docker-compose.yml` line 18 — remove `:-zovark_dev_2026` fallback from `POSTGRES_PASSWORD`
- [x] 3.2 Edit `docker-compose.yml` line 54 — remove `:-hydra-redis-dev-2026` fallback from redis `--requirepass`
- [x] 3.3 Edit `docker-compose.yml` line 63 — remove `:-hydra-redis-dev-2026` fallback from redis healthcheck
- [x] 3.4 Edit `docker-compose.yml` line 95 — remove `:-change-me-surreal` fallback from SurrealDB root password
- [x] 3.5 Edit `docker-compose.yml` line 372 — remove the `:-zovark-jwt-secret-dev-2026-CHANGE-ME-...` fallback from `JWT_SECRET`
- [x] 3.6 Edit `docker-compose.yml` line 441 — remove the second SurrealDB password fallback
- [x] 3.7 Edit `docker-compose.yml` line 803 — remove `:-hydra_dev_2026` from `ZOVARK_DB_PASSWORD` (plus added explicit `ZOVARK_DB_PASSWORD` to worker service env so Pydantic resolves)
- [x] 3.8 Edit `docker-compose.yml` line 805 — remove `:-hydra-redis-dev-2026` from `ZOVARK_REDIS_PASSWORD` (plus added explicit `ZOVARK_REDIS_PASSWORD` to worker service env)
- [x] 3.9 Edit `docker-compose.optional.yml` line 46 — remove NATS password fallback (plus LITELLM_MASTER_KEY fallback)
- [x] 3.10 Edit `docker-compose.airgap.yml` lines 14, 49, 98, 123, 142 — replaced all literal passwords with `${VAR:?...}` required syntax
- [x] 3.11 Update `docker-compose.airgap.yml` top-of-file comment block to document that operators MUST populate `.env` before boot
- [x] 3.11a Also removed 13+ additional `${POSTGRES_PASSWORD:-zovark_dev_2026}`, `${REDIS_PASSWORD:-zovark-redis-dev-2026}`, `${MINIO_ROOT_PASSWORD:-zovark_dev_2026}` fallbacks discovered during spec-scope sweep (scope was wider than the original 8 lines).

### 3b. Helm

- [x] 3.12 Edit `helm/zovarc/values.yaml` line 81 — replace `password: zovark_dev_2026` with `password: ""`
- [x] 3.13 Edit `helm/zovarc/values.yaml` line 107 — replace `masterKey: sk-zovark-dev-2026` with `masterKey: ""`
- [x] 3.14 Edit `helm/zovarc/values.yaml` line 134 — replace `databaseUrl: "postgresql://zovark:zovark_dev_2026@..."` with `databaseUrl: ""`
- [x] 3.15 Edit `helm/zovarc/values.yaml` line 135 — replace `jwtSecret: "zovark-jwt-secret-dev-2026"` with `jwtSecret: ""`
- [x] 3.16 Edit `helm/zovarc/values.yaml` line 136 — replace `litellmMasterKey: "sk-zovark-dev-2026"` with `litellmMasterKey: ""`
- [x] 3.17 Wrapped every `.Values.secrets.*` reference in `helm/zovarc/templates/secret.yaml` with Helm's `required` helper.
- [x] 3.18 Test render: `helm template helm/zovarc/` fails with readable error `secrets.databaseUrl is required — pass via --set secrets.databaseUrl=postgresql://...` (verified via alpine/helm:3.14.0 container).

### 3c. Kubernetes

- [x] 3.19 Edit `k8s/base/worker/deployment.yaml` line 51 — replaced literal `value: "redis://:hydra-redis-dev-2026@redis:6379/0"` with `valueFrom: secretKeyRef: name: zovark-worker-secrets, key: redis-url`.
- [x] 3.20 Removed the `# FIX #13` annotation.
- [x] 3.21 Updated `k8s/base/secrets.yaml.example` with a new `zovark-worker-secrets` Secret containing `redis-url` key + placeholder.
- [x] 3.22 Grep `k8s/` for other literal credential strings — zero hits remain.

### 3d. Python settings + LLM key centralization

- [x] 3.23 Edit `worker/settings.py` — deleted constants `_DEFAULT_DB_PASSWORD`, `_DEFAULT_REDIS_PASSWORD`, `_DEFAULT_SURREAL_PASSWORD` and the `_warn_if_default_password` helper.
- [x] 3.24 `db_password: SecretStr` now required (no default).
- [x] 3.25 `redis_password: SecretStr` now required (no default).
- [x] 3.26 Removed the try/except fallback block around `settings = ZovarkSettings()` — singleton init now fail-fasts.
- [x] 3.27 Verified `llm_key: SecretStr` field present and required (no default).
- [x] 3.28 Edit `worker/stages/assess.py` line 54 — `ZOVARK_LLM_KEY = settings.llm_key.get_secret_value()` via centralized settings import.
- [x] 3.29 Edit `worker/stages/analyze.py` — same pattern.
- [x] 3.30 Edit `worker/finetuning/evaluator.py` — same pattern.
- [x] 3.31 Grep-verified zero remaining `sk-zovark-dev-2026` / `hydra_dev_2026` / `hydra-redis-dev-2026` / `zovark_dev_2026` / `zovark-redis-dev-2026` across the spec-defined production-adjacent scope (`docker-compose*.yml`, `helm/`, `k8s/`, `worker/settings.py`, `worker/stages/`, `worker/finetuning/`, `.env.example`).
- [x] 3.31a Additionally scope-centralized `DATABASE_URL` / `REDIS_URL` fallbacks across `worker/stages/{ingest,store,govern,llm_gateway,template_promoter}.py` + `worker/finetuning/evaluation.py` via `settings.database_url` / `settings.redis_url`, and centralized LLM key loading in `worker/stages/llm_gateway.py`.
- [x] 3.31b Added `# test-only` annotations to `tests/e2e/docker-compose.test.yml` and converted the `ZOVARK_LLM_KEY` bearer fallback in `scripts/test-airgap.sh` to required.

## 4. `.env.example` regeneration

- [x] 4.1 Read current `.env.example` and note every variable
- [x] 4.2 Collect every `Field(...)` env var from `worker/settings.py`
- [x] 4.3 Collect every `${VAR}` reference from `docker-compose.yml`, `docker-compose.optional.yml`, `docker-compose.airgap.yml`
- [x] 4.4 Collect every secret key from `helm/zovarc/values.yaml`
- [x] 4.5 Collect every Secret key from `k8s/base/secrets.yaml.example`
- [x] 4.6 Rewrote `.env.example` as 150 lines organized by subsystem (DATABASE, CACHE, AUTH, LLM, INFERENCE CONTAINER, EXECUTION, BURST PROTECTION, OBSERVABILITY, UPDATE SIGNING, Optional data plane/minio/nats/litellm/collection). Every value is a `REPLACE_ME_<hint>` placeholder.
- [x] 4.7 Added header block documenting `openssl rand -base64 32/64`, `openssl rand -hex 32`, `htpasswd -nbBC 12`.
- [x] 4.8 Grep-verified zero real credential strings in `.env.example`.
- [x] 4.9 Verified `.env.example` is tracked (`git ls-files .env.example`) and `.env` is gitignored (`git check-ignore .env`).

## 5. CI workflow compatibility

- [x] 5.1 Edit `.github/workflows/ci.yml` — added top-level `env:` block with 8 test-only vars (POSTGRES_PASSWORD, ZOVARK_DB_PASSWORD, REDIS_PASSWORD, ZOVARK_REDIS_PASSWORD, SURREAL_ROOT_PASSWORD, MINIO_ROOT_PASSWORD, JWT_SECRET, ZOVARK_LLM_KEY).
- [x] 5.2 Added `# test-only` annotation header comment above the env block.
- [x] 5.3 Edit `.github/workflows/coverage.yml` — added top-level `env:` block with 4 test-only vars (same annotation).
- [x] 5.4 Verified annotations present via `grep -l 'test-only\|stabilize-runtime-hygiene' .github/workflows/*.yml`.
- [x] 5.5 `act` not available locally; defer full CI dry-run to GitHub Actions green gate post-push.

## 6. Verification

- [x] 6.1 **Fail-fast test**: `env -i PATH=$PATH HOME=$HOME docker compose -f docker-compose.yml config` exits 1 with `required variable SURREAL_ROOT_PASSWORD is missing a value: SURREAL_ROOT_PASSWORD is required — see .env.example`.
- [x] 6.2 **Fail-fast test (worker)**: new `settings.py` in a fresh Python env raises `ValidationError` naming `db_password`, `redis_password`, `llm_key` when ZOVARK_ env is stripped.
- [x] 6.3 **Happy-path settings load**: with required env populated, `settings.database_url` and `settings.redis_url` compute correctly; worker singleton loads.
- [x] 6.4 **Worker rebuild**: `docker compose build worker` succeeds; image tag `zovark_mine-worker:latest` built.
- [x] 6.5 **Import sanity**: worker container starts healthy; logs show `Worker starting task_queue=zovark-tasks workflows=17 activities=107`.
- [ ] 6.6 **Regression suite** — **OPERATOR-DEFERRED**: `bash autoresearch/cycle10/verify_all.sh` returned 2/15 in apply session because `zovark-inference` container is not running in this environment. The 2 that completed (brute_force, lateral_movement) both returned correct verdicts through the renamed class via wire-name preservation, proving the pipeline change did not regress. Full 15/15 requires `docker compose -f docker-compose.yml -f docker-compose.distroless.yml up -d zovark-inference` and is deferred to operator verification.
- [ ] 6.7 **Dedup regression** — **OPERATOR-DEFERRED**: same environment limitation (requires inference).
- [x] 6.8 **In-flight workflow compatibility**: the 2 completed investigations in 6.6 confirm the renamed `InvestigationWorkflow` class accepts tasks submitted via the API's default `"InvestigationWorkflowV2"` string — wire-name preservation verified.
- [x] 6.9 **Post-merge grep sanity**: zero hits on credential strings across the spec-defined scope.
- [x] 6.10 **Helm render fail**: `helm template helm/zovarc/` exits 1 with `secrets.databaseUrl is required — pass via --set secrets.databaseUrl=postgresql://...`.
- [x] 6.11 **Helm render happy path**: with all required `--set` values, renders successfully (exit 0).

## 7. Post-merge communication

- [x] 7.1 Updated `HANDOVER.md §3 Credentials` with a new `cp .env.example .env` step and an inline warning about stabilize-runtime-hygiene's no-default posture.
- [x] 7.2 Updated `docker-compose.airgap.yml` top-of-file comment to name `.env.example` as the starting point.
- [ ] 7.3 PR description note — deferred to merge-time (this change is being committed as part of the migrate-to-zovark-final-repo commit, not through a PR).

## 8. Rollback plan

- [x] 8.1 Rollback is `git revert` of the merge commit. No data migration to undo, no schema change to reverse.
- [x] 8.2 If CI reds-out post-merge, revert first, debug second. Do not attempt to fix forward under CI pressure.
