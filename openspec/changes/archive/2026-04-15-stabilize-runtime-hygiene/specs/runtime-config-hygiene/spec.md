## ADDED Requirements

### Requirement: No hardcoded credential fallbacks in production-adjacent config
The repository SHALL NOT contain hardcoded credential fallback defaults in any production-adjacent configuration file. This applies to `docker-compose.yml`, `docker-compose.optional.yml`, `docker-compose.airgap.yml`, `helm/zovarc/values.yaml`, `k8s/base/**/*.yaml`, and `worker/settings.py`. Test fixtures under `tests/`, `worker/tests/`, `migrations/seed_dev_data.sql`, and `.github/workflows/` are exempt PROVIDED they are annotated with a `# test-only` comment naming the context.

#### Scenario: Grep returns zero hits in production-adjacent files
- **WHEN** a reviewer runs `rg 'hydra_dev_2026|hydra-redis-dev-2026|sk-zovark-dev-2026' docker-compose*.yml helm/ k8s/ worker/settings.py worker/stages/ worker/finetuning/`
- **THEN** zero matches are returned

#### Scenario: Compose substitution has no default
- **WHEN** a reviewer runs `rg ':\-' docker-compose.yml docker-compose.optional.yml docker-compose.airgap.yml` looking for `${VAR:-default}` patterns in credential-bearing lines
- **THEN** no credential, secret, password, key, or token field is matched

### Requirement: Fail-fast startup on missing environment variables
When any required credential environment variable is missing, the stack SHALL fail to start with a readable error that names the specific missing variable. Failure SHALL be surfaced at two layers: (a) Docker Compose SHALL fail substitution for required `${VAR}` references, (b) the Python worker SHALL raise a Pydantic `ValidationError` at Settings load time.

#### Scenario: Compose fails with missing POSTGRES_PASSWORD
- **WHEN** an operator runs `env -i PATH=$PATH docker compose up -d postgres`
- **THEN** the command exits non-zero and stderr contains a message identifying `POSTGRES_PASSWORD` (or an equivalent named variable) as unset

#### Scenario: Worker fails with missing LLM key
- **WHEN** the worker container starts with `ZOVARK_DB_PASSWORD` and `ZOVARK_REDIS_PASSWORD` set but `ZOVARK_LLM_KEY` unset
- **THEN** the worker process exits non-zero and the container logs contain a Pydantic `ValidationError` naming the `llm_key` field

#### Scenario: Happy-path startup succeeds
- **WHEN** an operator runs `docker compose up -d` with a `.env` populated from `.env.example` with real values
- **THEN** all core services reach the `healthy` state within 120 seconds and `curl -sf http://localhost:8090/ready` returns HTTP 200

### Requirement: Centralized LLM key loading
All pipeline code that reads `ZOVARK_LLM_KEY` SHALL do so through a single accessor: `settings.llm_key.get_secret_value()`, where `settings` is imported from `worker/settings.py`. No pipeline file SHALL call `os.environ.get("ZOVARK_LLM_KEY", ...)` with a fallback literal.

#### Scenario: No hardcoded LLM key fallbacks remain
- **WHEN** a reviewer runs `rg 'ZOVARK_LLM_KEY.*sk-' worker/`
- **THEN** zero matches are returned

#### Scenario: Rotating the key requires one env change only
- **WHEN** an operator rotates `ZOVARK_LLM_KEY` by editing `.env` and restarting the worker
- **THEN** all three former call sites (`assess.py`, `analyze.py`, `evaluator.py`) pick up the new value without any code edits

### Requirement: `.env.example` is the authoritative template
The file `.env.example` SHALL be present at the repository root, tracked in git, and SHALL contain a placeholder entry for every environment variable that `worker/settings.py`, `docker-compose*.yml`, `helm/zovarc/values.yaml`, or `k8s/base/**/*.yaml` consume. Every placeholder value SHALL be explicitly non-functional if pasted unchanged (e.g. `REPLACE_ME_openssl_rand_base64_32`).

#### Scenario: `.env.example` exists and is tracked
- **WHEN** a reviewer runs `git ls-files .env.example`
- **THEN** the command returns `.env.example`

#### Scenario: No real credential strings in `.env.example`
- **WHEN** a reviewer runs `rg 'hydra_dev_2026|hydra-redis-dev-2026|sk-zovark-dev-2026|TestPass2026' .env.example`
- **THEN** zero matches are returned

#### Scenario: Every settings.py env var is represented
- **WHEN** a reviewer enumerates every `Field(...)` declaration in `worker/settings.py` with a `ZOVARK_`-prefixed env name
- **THEN** each corresponding variable name appears in `.env.example`

### Requirement: Temporal workflow wire name is preserved
When the `InvestigationWorkflowV2` Python class is renamed to `InvestigationWorkflow`, the Temporal wire name `"InvestigationWorkflowV2"` SHALL be preserved via an explicit `@workflow.defn(name="InvestigationWorkflowV2")` decorator. Existing callers that pass the string `"InvestigationWorkflowV2"` as the workflow type SHALL continue to resolve correctly.

#### Scenario: Redpanda consumer default still resolves
- **WHEN** a task is submitted with no explicit `workflow` field and `redpanda_consumer.py` falls back to the default `"InvestigationWorkflowV2"` string
- **THEN** Temporal successfully routes the workflow to the renamed Python class and the workflow runs to completion

#### Scenario: Python class symbol is the new name
- **WHEN** a reviewer runs `rg 'class InvestigationWorkflow' worker/stages/investigation_workflow.py`
- **THEN** the match shows `class InvestigationWorkflow:` (without the `V2` suffix)

### Requirement: K8s worker deployment uses Secret references for credentials
The file `k8s/base/worker/deployment.yaml` SHALL NOT contain literal credential strings in any `env:` `value:` field. All credential-bearing env vars SHALL use `valueFrom: secretKeyRef:` referencing a Secret documented in `k8s/base/secrets.yaml.example`.

#### Scenario: No literal Redis DSN in the deployment
- **WHEN** a reviewer runs `rg 'redis://.*@redis' k8s/base/worker/deployment.yaml`
- **THEN** zero matches are returned

#### Scenario: `secrets.yaml.example` documents the referenced keys
- **WHEN** a reviewer opens `k8s/base/secrets.yaml.example`
- **THEN** the file contains a Secret manifest named `zovark-worker-secrets` with a `redis-url` key and a placeholder value

### Requirement: Helm chart requires explicit secret values
The file `helm/zovarc/values.yaml` SHALL NOT contain non-empty default values for credential keys (`postgres.password`, `litellm.masterKey`, `litellm.litellmMasterKey`, `jwt.secret`, `postgres.databaseUrl`, or any field whose name contains `password`, `secret`, `token`, or `key` and whose semantics are a credential). The corresponding Helm templates SHALL wrap every such reference with Helm's `required` helper function so `helm install` fails with a readable error naming each missing value.

#### Scenario: Default render fails with a readable error
- **WHEN** a reviewer runs `helm template helm/zovarc/`
- **THEN** the command exits non-zero and stderr contains a message naming at least one required secret field

#### Scenario: Override-supplied render succeeds
- **WHEN** a reviewer runs `helm template helm/zovarc/` with `--set` flags providing every required credential
- **THEN** the command exits zero and produces valid Kubernetes manifests

### Requirement: Dead and orphan Python files are removed
The repository SHALL NOT retain `worker/nats_consumer.py`. Additionally, `worker/redis_client.py` SHALL be removed after its one caller (`decrement_active`) is inlined into `worker/_legacy_activities.py`. Additional orphan Python files (`worker/context_manager.py`, `worker/investigation_cache.py`, `worker/rate_limiter.py`) SHALL be removed if and only if a repository-wide string-based import sweep confirms zero importers.

#### Scenario: `nats_consumer.py` is gone
- **WHEN** a reviewer runs `test -f worker/nats_consumer.py`
- **THEN** the command exits non-zero

#### Scenario: `redis_client.py` is gone and `_legacy_activities.py` still has `decrement_active`
- **WHEN** a reviewer runs `test -f worker/redis_client.py && echo exists || echo gone`
- **THEN** the output is `gone`
- **AND WHEN** the reviewer runs `rg 'def decrement_active' worker/_legacy_activities.py`
- **THEN** the function definition is present

### Requirement: `worker/activities/__init__.py` import style is documented
The file `worker/activities/__init__.py` SHALL import shared symbols from `_legacy_activities` as a top-level module (`from _legacy_activities import …`). This is correct because `worker/` is not a Python package (no `worker/__init__.py`) and `_legacy_activities.py` is a sibling of `worker/activities/`. The file SHALL include a comment block explaining why the bare import is intentional so future readers do not "fix" it to a relative import.

#### Scenario: Comment block explains the bare import
- **WHEN** a reviewer opens `worker/activities/__init__.py`
- **THEN** the file contains a comment block noting that `worker/` is not a package and that the bare import is intentional

#### Scenario: Relative-import "fix" is not applied
- **WHEN** a reviewer runs `rg 'from \._legacy_activities import' worker/activities/__init__.py`
- **THEN** zero matches are returned
