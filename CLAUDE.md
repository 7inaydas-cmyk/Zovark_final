# Zovark v3.2.1 — Autonomous AI SOC Agent

> **Engineering Discipline:** All Claude Code sessions must follow `ENGINEERING_DISCIPLINE.md`.
> Load it at session start. Use slash commands for all work.

> Air-gapped SOC investigation platform for regulated enterprises (GDPR/HIPAA/CMMC).
> Receives SIEM alerts, runs deterministic tool-based investigations, delivers structured verdicts.
> Rebranded from HYDRA. All source code uses "Zovark" branding.

## Quick Reference

| Field | Value |
|-------|-------|
| Version | v3.2.1 on v3.1-hardening branch (calibration, dedup redesign, SIEM push-back, Valkey, telemetry engine) |
| Date | 2026-04-04 |
| Status | Production-ready — 40 tools, 24 plans, 100% detection, investigation-aware dedup, SIEM push-back, Valkey cache |
| Stack | Go API + Python Temporal Worker + React Dashboard + PostgreSQL/pgvector + Valkey (BSD) + LLM inference |
| Models | Gemma 4 E4B Q4_K_M (dev, both roles, --ctx-size 4096). Customer: same FAST + bigger CODE model (8B/13B/70B). |
| LLM Host | llama-server (llama.cpp) in container `zovark-inference`. No litellm. Singleton httpx client with dual semaphores (FAST/CODE). |
| Pipeline | V3 6-stage — deterministic tools + governance layer (v2 sandbox behind feature flag) |
| Tools | 40 investigation tools (7 categories) + 24 saved investigation plans |
| Templates | 25 active (12 hand-written + 2 flywheel + 10 AutoResearch + 1 quorum-promoted) |
| Tests | 535 unit + 14 integration + 515-alert corpus |
| Services | 10 core Docker containers + optional profiles (tracing, monitoring, siem-lab, etc.) + zvadmin host CLI |
| Dashboard | React 19 + TypeScript + Vite 7 + Tailwind 4, 17 pages, SOC War Room design |
| Database | PostgreSQL 16 + pgvector, 86+ tables, 64 migrations, RLS on 10 tables |
| Concurrency | 16 concurrent activities, 32 concurrent workflows, Semaphore(2) on LLM calls |
| Feature Flag | `ZOVARK_EXECUTION_MODE=tools` (v3, default) or `sandbox` (v2 legacy) |
| Observability | OpenTelemetry → Signoz (self-hosted ClickHouse). `docker compose --profile tracing up -d` |
| Config | Pydantic Settings (`worker/settings.py`), SecretStr credentials, .env support |

## Credentials

| Resource | Credential |
|----------|------------|
| Admin login | admin@test.local / TestPass2026 (tenant `…0010`, user `…0020`) |
| Analyst login | analyst2@test.local / TestPass2026 (tenant `…0010`, user `…0021`) |
| Database | user=zovark, password=hydra_dev_2026, db=zovark |
| Redis | password=hydra-redis-dev-2026 |
| LLM endpoint | `ZOVARK_LLM_ENDPOINT=http://zovark-inference:8080/v1/chat/completions` |
| LLM key | `ZOVARK_LLM_KEY=sk-zovark-dev-2026` |
| JWT | 30-minute access tokens |

Fixture users are seeded automatically on fresh boot by `migrations/seed_dev_data.sql`
(mounted into postgres at `/docker-entrypoint-initdb.d/02-seed-dev.sql`). To re-seed
manually after wiping or accidentally deleting: `scripts/seed_dev.sh`. To verify the
fixture state without running the e2e probe: `scripts/seed_dev.sh --check`.
The reserved UUIDs `…0010` (dev tenant), `…0020` (admin), `…0021` (analyst) can be
referenced as literals in tests and scripts — they are guaranteed stable across
fresh volumes.

DB and Redis passwords were intentionally not renamed during the rebrand (`hydra_dev_2026`, `hydra-redis-dev-2026`). Use these exact values in all docker/psql/redis-cli commands.

---

## Architecture — V3 Investigation Pipeline (Default)

```
SIEM Alert --> Go API (:8090) --> Temporal --> InvestigationWorkflowV2

  Stage 1 INGEST    [NO LLM]  sanitize (25 patterns + Unicode) -> normalize -> batch
                               -> dedup (Redis) -> PII mask -> skill retrieval
                               -> content-based attack scan (54 patterns)
  Stage 2 ANALYZE   [LLM opt] Saved plan exists? -> Load plan (no LLM, ~5ms)
                               No plan? -> LLM selects tools (3B model, ~30s)
                               Loads institutional knowledge for LLM context
  Stage 3 EXECUTE   [NO LLM]  In-process tool runner — NO Docker sandbox
                               34 deterministic Python functions
                               Conditional branching ($step2 > 100)
                               Per-tool 5s timeout, total 30s timeout, error isolation
  Stage 4 ASSESS    [LLM opt] Verdict derivation -> IOC validation -> signal boost
                               Suppression detection -> provenance validation
                               Plain-English summary
  Stage 4.5 GOVERN  [NO LLM]  Autonomy check (observe/assist/autonomous)
                               Determines needs_human_review
  Stage 5 STORE     [NO LLM]  agent_tasks + investigations + audit_events
                               path_taken + plan_executed + execution_mode stored

  --> Structured Verdict: findings, IOCs (with evidence_refs), risk_score, verdict, MITRE ATT&CK, summary
```

### Tool Categories (40 tools)

| Category | Count | Examples |
|----------|-------|---------|
| Extraction | 8 | extract_ipv4, extract_domains, extract_hashes, extract_cves |
| Analysis | 4 | count_pattern, calculate_entropy, detect_encoding, check_base64 |
| Parsing | 5 | parse_windows_event, parse_syslog, parse_auth_log |
| Scoring | 6 | score_brute_force, score_phishing, score_c2_beacon |
| Detection | 12 | detect_kerberoasting, detect_ransomware, detect_lolbin_abuse, detect_com_hijacking, detect_encoded_service, detect_token_impersonation, detect_appcert_dlls, detect_dns_exfiltration |
| Enrichment | 4 | map_mitre, correlate_with_history, lookup_institutional_knowledge |

### Investigation Plans (24 attack types)

Saved in `worker/tools/investigation_plans.json`. Each plan: 2-8 tool steps with variable resolution and conditional branching. Plans cover: brute_force, phishing, ransomware, kerberoasting, golden_ticket, dcsync, dll_sideloading, lolbin_abuse, process_injection, c2, data_exfil, dns_exfiltration, powershell_obfuscation, and more.

### Governance Layer

| Level | Behavior |
|-------|----------|
| observe | All investigations need analyst review (default) |
| assist | Only non-benign need review |
| autonomous | Only edge cases (inconclusive, error) need review |

Config: `governance_config` table (tenant_id + task_type). API: `GET/PUT /api/v1/governance/config`.

### Feature Flag: Execution Mode

`ZOVARK_EXECUTION_MODE` environment variable:
- `tools` (default) — v3 deterministic tool-calling pipeline
- `sandbox` — v2 Docker sandbox pipeline (all v2 code preserved)

---

## Architecture — V2 Investigation Pipeline (Legacy, behind feature flag)

```
SIEM Alert --> Go API (:8090) --> Temporal --> InvestigationWorkflowV2

  Stage 1 INGEST   [NO LLM]  sanitize (25 patterns + Unicode normalization) -> normalize -> batch
                              -> dedup (Redis) -> PII mask -> skill retrieval
                              -> content-based attack scan (54 patterns) overrides benign routing
  Stage 2 ANALYZE  [LLM opt] Path A: template fast-fill (~350ms, no LLM)
                              Path B: template + LLM param fill (~30s, FAST model)
                              Path C: full LLM code generation (~120s, CODE model)
                              Benign: benign-system-event template (~350ms, no LLM)
  Stage 3 EXECUTE  [NO LLM]  4-layer AST prefilter (allowlist) -> Docker sandbox
                              (network=none, read-only, seccomp, 512MB, 64 PIDs, 120s timeout)
                              Safety wrapper on Path C (guarantees JSON on crash, risk=0 not 50)
  Stage 4 ASSESS   [LLM opt] Verdict derivation -> IOC extraction -> evidence_refs -> MITRE mapping
                              Signal boost (8 regex patterns) -> risk floor -> learning gate
                              IOC provenance validation -> suppression phrase detection
                              Plain-English summary -> validation override
                              Benign: risk<=35 -> benign unconditionally
                              LLM down: fail-closed -> needs_manual_review (never benign)
  Stage 5 STORE    [NO LLM]  agent_tasks + investigations + audit_events + investigation_memory
                              path_taken + generated_code stored for flywheel

  --> Structured Verdict: findings, IOCs (with evidence_refs), risk_score, verdict, MITRE ATT&CK, summary
```

### Four Code Paths

| Path | Trigger | Speed | LLM Model | Example |
|------|---------|-------|-----------|---------|
| A (saved plan) | task_type matches investigation plan | ~5ms | None | brute_force, phishing, ransomware |
| B (template + LLM fill) | template + LLM param extraction | ~30s | FAST (Nemotron-Mini-4B) | lateral_movement with enriched SIEM |
| C (LLM tool select) | no matching plan | ~2-10s | FAST (Nemotron-Mini-4B) | novel attack types |
| Benign | task_type matches benign-system-event | ~350ms | None | password_change, windows_update, health_check |

### Two-Model Routing

Zovark routes LLM calls to different models based on task complexity:

| Pipeline Stage | Model | Env Var | Why |
|---------------|-------|---------|-----|
| Path B (param fill) | Nemotron-Mini-4B | `ZOVARK_MODEL_FAST` | Simple JSON extraction, speed matters |
| Tool selection (no saved plan) | Nemotron-Mini-4B | `ZOVARK_MODEL_FAST` | Selects tools from catalog. V3 replacement for Path C code gen. |
| Assess (verdict + summary) | Nemotron-Mini-4B (dev) / 8B+ (customer) | `ZOVARK_MODEL_CODE` | Needs reasoning quality for risk calibration, IOC extraction |
| Path A (saved plan) | None | -- | Plan loaded from investigation_plans.json, no LLM |
| Benign routing | None | -- | Inverted logic, no LLM |

Why American models: Target customers (US defense/CMMC, healthcare/HIPAA) reject Chinese model provenance. Previously used Qwen 2.5 14B (Alibaba). Switched to NVIDIA Nemotron (American, open weights). All Qwen/Alibaba references removed.

Dual-endpoint opt-in via `ZOVARK_LLM_ENDPOINT_FAST` and `ZOVARK_LLM_ENDPOINT_CODE`. Dev tier: both point to same container. Customer tier: separate containers for FAST (high concurrency) and CODE (high quality).

### Benign Routing (Inverted Logic + Content Override)

`worker/stages/ingest.py` has `ATTACK_INDICATORS` list (40 terms). If task_type/rule_name/title do NOT match any attack indicator, the alert routes to the `benign-system-event` skill template (31 benign task types registered). Novel benign alerts default to benign, not to expensive LLM Path C.

**Content-based override (red team patch):** Even if metadata is benign, `_has_raw_log_attack_content()` scans raw_log against 54 high-confidence attack patterns. If attack content is found, benign routing is blocked and the alert is forced to Path C investigation. This prevents classification evasion attacks where an attacker uses benign metadata but includes real attack commands in the log data.

### Key Calibration Logic

- **Assess prompt anchors** (`dpo/prompts_v2.py`): Concrete risk reference points for Llama models (e.g., brute force 500+ attempts = risk 95-100, phishing URL = risk 80-90)
- **Attack signal boost** (`assess.py`): 8 regex patterns (SQLi, XSS, path traversal, etc.) add risk when found in SIEM data
- **Template attack risk floor** (`assess.py`): If task_type matches known attack indicator AND risk is underscored, boost to threshold. Prevents LLM under-scoring.
- **Learning gate** (`assess.py`): Path C results are flagged `needs_analyst_review` for human feedback loop
- **Validation override** (`assess.py`): If output validator flags `needs_manual_review` but risk >= 70, override to `true_positive`
- **Prose stripping** (`analyze.py` `_scrub_code()`): Llama 8B wraps code in prose ("Here is the Python code..."). Scrubber detects first Python-like line and strips everything before/after.
- **Plain-English summary** (`assess.py`): Human-readable investigation summary generated alongside structured verdict
- **IOC provenance validation** (`assess.py`): IOCs from structured fields without raw_log backing are downgraded to `confidence=low`. Prevents phantom IP fabrication attacks.
- **Suppression phrase detection** (`assess.py`): 9 patterns detect adversarial risk manipulation (e.g., "scheduled test", "do not escalate"). When suppression language + attack indicators appear together, risk is boosted to 75+ instead of lowered.
- **Fail-closed LLM degradation** (`analyze.py` + `assess.py`): When LLM is unavailable, Path C alerts get `verdict=needs_manual_review` (never benign). Circuit breaker goes RED. Path A/benign continue normally.

---

## Key Files

### V3 Tool Library (Python)

| File | Purpose |
|------|---------|
| `worker/tools/extraction.py` | 8 IOC extraction tools (IPv4, IPv6, domains, URLs, hashes, emails, usernames, CVEs) |
| `worker/tools/analysis.py` | 4 analysis tools (pattern count, entropy, encoding detection, base64) |
| `worker/tools/parsing.py` | 5 log parsing tools (Windows events, syslog, auth, DNS, HTTP) |
| `worker/tools/scoring.py` | 6 risk scoring tools (brute force, phishing, lateral movement, exfil, C2, generic) |
| `worker/tools/detection.py` | 7 composite detection tools (kerberoasting, golden ticket, ransomware, phishing, C2, exfil, LOLBin) |
| `worker/tools/enrichment.py` | 4 enrichment tools (MITRE mapping, known-bad lookup, correlation, institutional knowledge) |
| `worker/tools/catalog.py` | Tool catalog — maps 40 tool names to functions, descriptions, args |
| `worker/tools/runner.py` | Tool runner — executes plans with variable resolution, conditional branching, timeouts |
| `worker/tools/investigation_plans.json` | 24 saved investigation plans for all attack types |
| `worker/stages/govern.py` | Stage 4.5: Governance — autonomy slider (observe/assist/autonomous) |

### v3.1 Hardening (Python)

| File | Purpose |
|------|---------|
| `worker/settings.py` | Pydantic Settings — centralized config, SecretStr credentials, .env support, ZOVARK_ env prefix |
| `worker/schemas.py` | LLM output validation — VerdictOutput, IOCItem, ToolSelectionOutput with safe fallbacks |
| `worker/llm_client.py` | Singleton httpx.AsyncClient, asyncio.Semaphore(2), OTEL spans per LLM call |
| `worker/events.py` | PostgreSQL NOTIFY event emitter — fire-and-forget streaming waterfall events |
| `worker/tracing.py` | OpenTelemetry init — graceful degradation if Signoz unreachable |
| `config/signoz/otel-collector-config.yaml` | OTLP receivers → ClickHouse exporters for traces/metrics/logs |
| `config/signoz/clickhouse-cluster.xml` | Single-node ClickHouse with built-in Keeper for Signoz |
| `config/signoz/frontend-nginx.conf` | Nginx proxy — prefix match for /api/* to query service |
| `dashboard/src/pages/TaskList.tsx`, `TaskDetail.tsx` | `EventSource` clients for `GET /api/v1/tasks/stream` — live task completion / investigation events |

### Worker Pipeline (Python)

| File | Purpose |
|------|---------|
| `worker/stages/ingest.py` | Stage 1: sanitize, normalize, batch, dedup, PII mask, skill retrieval |
| `worker/stages/analyze.py` | Stage 2: v3 tool plan loading OR v2 code gen (feature flag), institutional knowledge |
| `worker/stages/execute.py` | Stage 3: v3 in-process tool runner OR v2 Docker sandbox (feature flag) |
| `worker/stages/assess.py` | Stage 4: verdict, IOC extraction, signal boost, learning gate, plain-English summary |
| `worker/stages/store.py` | Stage 5: DB writes with synchronous_commit, stores path_taken + generated_code |
| `worker/stages/llm_gateway.py` | Dual-endpoint routing (MODEL_FAST/MODEL_CODE), audit logging |
| `worker/stages/investigation_workflow.py` | InvestigationWorkflowV2 — 5-stage Temporal orchestrator |
| `worker/stages/input_sanitizer.py` | 25 injection patterns, Unicode normalization, field truncation, entropy analysis, tail scanning |
| `worker/stages/normalizer.py` | 70+ field mappings (Splunk/Elastic/firewall/legacy) |
| `worker/stages/smart_batcher.py` | Redis-backed, severity-aware batching windows |
| `worker/stages/circuit_breaker.py` | GREEN/YELLOW/RED states, hysteresis recovery |
| `worker/stages/code_cache.py` | Structural key hashing, 24h TTL, Redis-backed |
| `worker/stages/template_promoter.py` | Templatize, validate, promote Path C code to agent_skills |
| `worker/stages/output_validator.py` | Schema validation, IOC normalization |
| `worker/stages/mitre_mapping.py` | MITRE ATT&CK technique mapping |

### Go API

| File | Purpose |
|------|---------|
| `api/main.go` | Gin router, 90+ registered routes |
| `api/auth.go` | JWT (30min), OIDC/SSO, TOTP 2FA |
| `api/task_handlers.go` | Task CRUD, verdict/risk/path in list response |
| `api/promotion_handlers.go` | promotion-queue, analyst-feedback (2-person quorum), promotion-approve, auto-templates, dashboard-stats |
| `api/siem_ingest.go` | Splunk HEC + Elastic SIEM webhook ingest, trace_id generation, 3-layer funnel |
| `api/alert_dedup.go` | Layer 1: Pre-Temporal Redis dedup (hash-compatible with Python) |
| `api/batch_buffer.go` | Layer 2: Pre-Temporal batch buffer (Redis Lua script, 5s window) |
| `api/backpressure.go` | Layer 3: Temporal queue depth throttle + drain goroutine |
| `api/cipher_audit_handlers.go` | 5 cipher audit API endpoints |
| `api/admin_handlers.go` | Diagnostic export (.zvk zip with secret scrubbing) |
| `api/compliance_handlers.go` | CMMC compliance evidence report (IR controls mapping) |
| `api/sse.go` | SSE real-time task updates (global stream + per-task) |
| `api/handlers.go` | Health check + readiness probe (GET /ready) |
| `api/db.go` | Connection pool, `beginTenantTx()` for RLS tenant context |

### v3.2.1 New Files

| File | Purpose |
|------|---------|
| `api/siem_pushback.go` | SIEM verdict push-back (Splunk HEC + Elastic + webhook) |
| `api/alert_dedup.go` | v2 investigation-aware dedup (severity escalation, failed retry, force reinvestigate) |
| `cmd/zvadmin/telemetry.go` | Shared telemetry data layer (OOB, PG, Redis, Docker, nvidia-smi) |
| `cmd/zvadmin/diagnose.go` | 8-check health diagnostic with operator actions |
| `cmd/zvadmin/alerts.go` | Pipeline statistics with verdict breakdown |
| `cmd/zvadmin/modelcheck.go` | Risk score calibration report |
| `cmd/zvadmin/deduphealth.go` | Dedup decision distribution and efficiency |
| `cmd/zvadmin/troubleshoot.go` | 5-symptom interactive troubleshooter |
| `cmd/zvadmin/update.go` | Model update with staging + rollback |
| `autoresearch/telemetry_driven/` | 6-module telemetry-driven AutoResearch engine |
| `migrations/063_system_tenant.sql` | System tenant for break-glass auth |
| `migrations/064_dedup_count.sql` | dedup_count column on agent_tasks |
| `LICENSES/` | Third-party license compliance (10 files) |

### Other Key Files

| File | Purpose |
|------|---------|
| `agent/healer.py` | Fleet agent v1.1: self-healer + Sneakernet UI + AI crash diagnosis + Signoz checks |
| `dpo/prompts_v2.py` | Full prompt library: system, task, tools, RAG, retry, scoring anchors (~900 LOC) |

---

## Investigation Code Cache

Redis-based cache for Path C generated code. Repeat alert patterns skip LLM entirely.

- **Key:** hash(task_type + rule_name + sorted SIEM field names) -- structural, not value-based
- **TTL:** 24 hours (configurable via `ZOVARK_CODE_CACHE_TTL`)
- **Flush after prompt updates:** `scripts/flush_code_cache.sh`
- **Implementation:** `worker/stages/code_cache.py`, integrated in `analyze.py`

---

## AST Prefilter Allowlist

Stage 3 uses a 4-layer AST prefilter. Only these standard library modules are permitted in generated investigation code:

`json`, `re`, `datetime`, `collections`, `math`, `hashlib`, `ipaddress`, `base64`, `urllib.parse`, `csv`, `statistics`, `string`, `copy`, `itertools`, `functools`, `typing`

Everything else (os, sys, subprocess, socket, eval, exec, etc.) is blocked before sandbox execution.

---

## Database

| Field | Value |
|-------|-------|
| Engine | PostgreSQL 16 + pgvector |
| Credentials | user=zovark, password=hydra_dev_2026, db=zovark |
| Tables | 84 (+ template_promotion_approvals) |
| Migrations | 64 files in `migrations/` |
| Connection pooling | PgBouncer (400 client / 25 server) |
| RLS | Enabled on 10 tenant-scoped tables (defense-in-depth) |
| Key tables | agent_tasks (has trace_id), investigations, agent_skills (25 templates), llm_audit_log, cipher_audit_events, audit_events (has trace_id), entities, entity_edges, detection_rules, response_playbooks, cross_tenant_entities, investigation_memory (SINGULAR name), template_promotion_approvals |

Apply migrations: `docker compose exec -T postgres psql -U zovark -d zovark < migrations/NNN_name.sql`

For batch application of all unapplied migrations against an existing volume, use `scripts/apply_migrations.sh` (ledger-aware, idempotent, gated on 068 SurrealDB cutover). See `docs/RUNBOOK_HEALTHCHECK.md#schema-drift`.

---

## Skill Templates (25 Active)

12 hand-written + 2 flywheel-promoted + 10 AutoResearch + 1 quorum-promoted:

| Slug | Types | Purpose |
|------|-------|---------|
| brute-force-investigation | 4 | Auth failure counting, credential stuffing, protocol detection |
| phishing-investigation | 3 | URL analysis, email headers, typosquatting, attachments |
| ransomware-triage | 3 | Shadow copy deletion, mass encryption, ransom notes |
| data-exfiltration-detection | 9 | Transfer volume, cloud storage, encoding, off-hours |
| privilege-escalation-hunt | 1 | Sudo/su, UAC bypass, SUID, token manipulation |
| c2-communication-hunt | 1 | Beacon intervals, DGA entropy, C2 signatures |
| lateral-movement-detection | 1 | PsExec/WMI/WinRM, pass-the-hash, admin shares |
| insider-threat-detection | 1 | Off-hours, bulk access, data staging, HR context |
| network-beaconing | 4 | Timestamp analysis, DNS anomalies, fixed payloads |
| cloud-infrastructure-attack | 1 | IAM changes, CloudTrail tampering, resource spikes |
| supply-chain-compromise | 1 | Hash mismatches, typosquatted packages, CI/CD mods |
| **benign-system-event** | **31** | Returns risk=15, verdict=benign for routine system operations |
| auto-credential_access-d58e8e | -- | Auto-promoted via flywheel (credential access patterns) |
| auto-golden_ticket-590d86 | -- | Auto-promoted via flywheel (golden ticket detection) |
| auto-kerberoasting-research | 2 | AutoResearch: RC4/0x17 TGS detection, SPN enumeration |
| auto-golden_ticket-research | 2 | AutoResearch: Forged TGT, abnormal lifetime, RC4 |
| auto-dcsync-research | 3 | AutoResearch: Directory replication from non-DC |
| auto-dll_sideloading-research | 3 | AutoResearch: Unsigned DLLs in suspicious paths |
| auto-lolbin_abuse-research | 3 | AutoResearch: certutil, mshta, bitsadmin abuse |
| auto-process_injection-research | 3 | AutoResearch: CreateRemoteThread, lsass targeting |
| auto-wmi_lateral-research | 3 | AutoResearch: Remote WMI process creation |
| auto-rdp_tunneling-research | 3 | AutoResearch: SSH tunnels, unusual RDP ports |
| auto-dns_exfiltration-research | 3 | AutoResearch: High-entropy DNS, TXT abuse |
| auto-powershell_obfuscation-research | 3 | AutoResearch: -enc, IEX, download cradles |
| auto-api_key_abuse-5d061a | 1 | Quorum-promoted: API key abuse from external IPs |

---

## Docker Services

### Core (10 services -- `docker compose up -d`)

| Service | Image | Port | Container | Healthcheck |
|---------|-------|------|-----------|-------------|
| postgres | pgvector/pgvector:pg16 | 5432 | zovark-postgres | pg_isready |
| redis | valkey/valkey:7-alpine | 6379 | zovark-redis | valkey-cli ping |
| pgbouncer | edoburu/pgbouncer | 6432 | zovark-pgbouncer | pg_isready |
| temporal | temporalio/auto-setup:1.24.2 | 7233 | zovark-temporal | -- |
| api | Custom Go build | 8090 | zovark-api | GET /ready (DB+Redis+Temporal) |
| worker | Custom Python build | -- | zovark-worker-1 | import check |
| dashboard | Custom React (nginx) | 3000 | zovark-dashboard | wget 127.0.0.1:3000 |
| healer | Python (agent/healer.py) | 8081 | zovark-healer | curl 127.0.0.1:8081/api/health |
| squid-proxy | ubuntu/squid | 3128 | zovark-egress-proxy | -- |
| docker-socket-proxy | tecnativa/docker-socket-proxy | 2375 | zovark-docker-proxy | -- |

### LLM Inference

- **Engine:** llama-server (llama.cpp) built from source in container `zovark-inference`
- **Model:** Nemotron-Mini-4B-Instruct Q4_K_M (2.6GB, fits in 4GB VRAM)
- **Start:** `docker compose -f docker-compose.yml -f docker-compose.distroless.yml up -d`
- Worker connects via `ZOVARK_LLM_ENDPOINT=http://zovark-inference:8080/v1/chat/completions`
- No litellm -- removed due to supply chain risk (PyPI compromise). Direct httpx POST to llama-server.
- Customer tier: dual containers via `docker-compose.enterprise.yml` (FAST + CODE on separate GPUs).

### Optional Profiles

| Profile | Services |
|---------|----------|
| tracing | Signoz (ClickHouse, OTEL collector, query service, frontend) |
| siem-lab | Elasticsearch, Kibana, Filebeat, Juice-Shop, nginx-proxy |
| monitoring | Prometheus, Grafana, postgres-exporter, redis-exporter |
| debug | temporal-ui |
| storage | MinIO |
| tls | Caddy |
| airgap-ollama | DEPRECATED — replaced by zovark-inference distroless container |

---

## Fleet Agent (agent/healer.py) — v1.1

Self-healing fleet agent running on port 8081:

- **Config:** Reads `ZOVARK_` prefixed env vars (aligned with `worker/settings.py`). Fallback to old names for backwards compat. No pydantic dependency (standalone container).
- **Sneakernet UI:** Embedded web interface for air-gapped deployments
- **AI crash diagnosis:** Uses 3B model to analyze container failures and suggest fixes
- **3-level escalation:** auto-restart -> AI diagnosis -> operator alert
- **Synthetic login check (60s):** POSTs to dashboard nginx proxy, auto-restarts dashboard on 502 (stale DNS cache fix)
- **Connectivity checks (60s):** Calls GET /ready on API (port 8090), checks LLM inference reachability. Auto-restarts API if DB connection lost.
- **Signoz health checks (OTEL-gated):** When `ZOVARK_OTEL_ENABLED=true`, checks ClickHouse (TCP 9000), OTEL collector (TCP 4318), Signoz query (`/api/v1/health`), Signoz frontend (`:3301`). When false, containers discovered but checks skipped.
- **Async health checks:** Uses httpx AsyncClient + asyncio.gather for concurrent checks (fixes Windows GIL blocking)
- **Daily reports:** Automated health summaries
- **Container:** zovark-healer

---

## Dashboard

| Field | Value |
|-------|-------|
| Stack | React 19 + TypeScript + Vite 7 + Tailwind 4 |
| Pages | 17 |
| Components | 16 |
| Design | SOC War Room -- #060A14 background, #00FF88 green accents, JetBrains Mono font |
| Port | 3000 (Docker/nginx), 5173 (dev) |

---

## Environment Variables

All variables have sensible defaults. Key configuration:

### Mode and Models

| Variable | Default | Purpose |
|----------|---------|---------|
| `ZOVARK_MODE` | `full` | `full` or `templates-only` (skip LLM entirely) |
| `ZOVARK_MODEL_FAST` | `nemotron-mini-4b` | FAST model for Path B param fill + Path C tool selection |
| `ZOVARK_MODEL_CODE` | `nemotron-mini-4b` | CODE model for Assess verdict + summary (customer tier: bigger model) |
| `ZOVARK_FAST_FILL` | `false` | Enable template fast-fill mode |

### LLM Endpoints

| Variable | Default | Purpose |
|----------|---------|---------|
| `ZOVARK_LLM_ENDPOINT` | `http://zovark-inference:8080/v1/chat/completions` | llama-server endpoint (both models on dev, FAST model on customer) |
| `ZOVARK_LLM_ENDPOINT_FAST` | same as above | FAST role endpoint (tool selection, param fill) |
| `ZOVARK_LLM_ENDPOINT_CODE` | same as above | CODE role endpoint (verdict, summary). Customer tier: separate container. |
| `ZOVARK_LLM_KEY` | `sk-zovark-dev-2026` | API key for LLM endpoint |

### Timeouts and Thresholds

| Variable | Default | Purpose |
|----------|---------|---------|
| `ZOVARK_ASSESS_TIMEOUT` | `45` | Seconds before assess stage times out |
| `ZOVARK_HUMAN_REVIEW_THRESHOLD` | `60` | Risk score threshold for human review flag |
| `ZOVARK_CB_YELLOW` | `50` | Circuit breaker yellow threshold |
| `ZOVARK_CB_RED` | `100` | Circuit breaker red threshold |
| `ZOVARK_CB_RECOVERY` | `25` | Circuit breaker recovery threshold |

### Batching and Caching

| Variable | Default | Purpose |
|----------|---------|---------|
| `ZOVARK_BATCH_WINDOW_SECONDS` | `60` | Smart batcher window |
| `ZOVARK_BATCH_MAX_SIZE` | `500` | Max alerts per batch |
| `ZOVARK_CODE_CACHE_TTL` | `86400` | Code cache TTL in seconds (24h) |
| `DEDUP_ENABLED` | `true` | Redis deduplication |

### Infrastructure

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | -- | PostgreSQL connection string |
| `REDIS_URL` | -- | Redis connection string |
| `REDIS_PASSWORD` | `hydra-redis-dev-2026` | Redis password |
| `JWT_SECRET` | -- | JWT signing secret |
| `TEMPORAL_ADDRESS` | -- | Temporal server address |

---

## Security Implementation

| Layer | What | Status |
|-------|------|--------|
| Input Sanitization | 25 injection patterns (template, code, SSTI), Unicode normalization (18 Cyrillic homoglyphs + zero-width), field truncation at 10K chars, Shannon entropy, tail scanning | IMPLEMENTED |
| Content-Based Routing | 54 RAW_LOG_ATTACK_PATTERNS override benign routing when attack content detected in raw_log | IMPLEMENTED |
| IOC Provenance | Validates IOCs against raw_log evidence. Phantom IPs downgraded to confidence=low | IMPLEMENTED |
| Suppression Detection | 9 patterns detect adversarial risk manipulation. Attack + suppression = risk boost to 75+ | IMPLEMENTED |
| Fail-Closed LLM | When LLM unavailable: verdict=needs_manual_review, circuit breaker RED. Never routes to benign | IMPLEMENTED |
| Docker Socket Proxy | tecnativa/docker-socket-proxy: only container lifecycle ops allowed. Images/exec/volumes/networks blocked (403) | IMPLEMENTED |
| Template Promotion Quorum | 2-person approval required for template promotion. Same analyst can't approve twice | IMPLEMENTED |
| Row-Level Security | RLS enabled on 10 tenant-scoped tables. `beginTenantTx()` sets tenant context. Defense-in-depth with WHERE clauses | IMPLEMENTED |
| Request Tracing | UUID trace_id generated at ingest, stored in agent_tasks + audit_events, propagated through pipeline | IMPLEMENTED |
| AST Prefilter | 4-layer allowlist -- only 16 safe stdlib modules permitted | IMPLEMENTED |
| Docker Sandbox | network=none, read-only, cap-drop ALL, 512MB, 64 PIDs, seccomp, 120s timeout (via socket proxy) | IMPLEMENTED |
| Safety Wrapper | Path C code wrapped in try/except, risk=0 on crash (not 50) | IMPLEMENTED |
| Code Scrubbing | Strips markdown fences, LLM tokens, prose wrapping from generated code | IMPLEMENTED |
| JWT Auth | 30-minute access tokens | IMPLEMENTED |
| RBAC | admin/analyst/viewer/api_key enforced in middleware | IMPLEMENTED |
| OIDC/SSO | Azure AD, Okta (api/auth.go) | IMPLEMENTED |
| TOTP 2FA | RFC 6238 | IMPLEMENTED |
| Audit Trail | audit_events table, monthly partitions | IMPLEMENTED |
| Synchronous Commit | Critical writes use `SET LOCAL synchronous_commit = on` | IMPLEMENTED |
| Evidence Citations | Every IOC has evidence_refs linking to source log line | IMPLEMENTED |
| Zero Hallucination | Prompt rules forbid inventing IOCs not in log data | IMPLEMENTED |
| Error Handling | `respondInternalError()` -- never expose Go errors to clients | IMPLEMENTED |
| Circuit Breaker | GREEN/YELLOW/RED states prevent cascading LLM failures | IMPLEMENTED |
| Investigation-Aware Dedup | v2 JSON entries in Redis with status/verdict/severity. Severity escalation bypass. Failed retry. Force reinvestigate. dedup_count column. | IMPLEMENTED |
| Batch Buffer | Redis Lua script groups (task_type, source_ip) in 5s window. 5000 same-IP → ~1 workflow per window. | IMPLEMENTED |
| Backpressure | Redis sorted set tracks workflow count. Soft limit (200) → queue. Hard limit (1000) → reject. Drain goroutine. | IMPLEMENTED |
| Learning Gate | Path C results flagged needs_analyst_review for human feedback | IMPLEMENTED |
| SIEM Verdict Push-Back | POST verdicts back to Splunk HEC / Elastic / generic webhook after investigation completes. Config via system_configs. | IMPLEMENTED |
| Pydantic LLM Validation | VerdictOutput (verdict enum, risk 0-100, MITRE regex), IOCItem (hash lengths, CVE format), ToolSelectionOutput (catalog check). Invalid → safe fallback. | IMPLEMENTED |
| Centralized Secrets | Pydantic Settings + SecretStr. No hardcoded passwords in pipeline stages. .env in .gitignore. | IMPLEMENTED |
| LLM Concurrency Control | Singleton httpx.AsyncClient + asyncio.Semaphore(2). Prevents GPU choke from 8 parallel activities. | IMPLEMENTED |

---

## API Routes (90+)

Key route groups on the Go API (port 8090):

| Group | Examples |
|-------|---------|
| Auth | login, register, refresh, OIDC callback, TOTP setup/verify |
| Tasks | CRUD, list with verdict/risk/path, batch submit |
| Approvals | approve/reject investigation actions |
| SIEM Ingest | POST /api/v1/ingest/splunk, POST /api/v1/ingest/elastic |
| Playbooks | SOAR response playbooks, approval gates |
| Skills | agent_skills CRUD, template management |
| Webhooks | external notification endpoints |
| Analytics | investigation stats, trend data |
| Feedback | analyst feedback on verdicts |
| Intelligence | cross-tenant entity resolution |
| Detection | Sigma rule management |
| Response | response playbook execution |
| Cipher Audit | 5 endpoints, NIST SP 800-57 compliance |
| Promotion Queue | promotion-queue, analyst-feedback (2-person quorum), promotion-approve, auto-templates, dashboard-stats |
| Compliance | POST /api/v1/compliance/report/cmmc — CMMC IR control mapping with evidence |
| Diagnostics | GET /api/v1/admin/diagnostics/export — .zvk zip with secret scrubbing |
| SSE Stream | GET /api/v1/tasks/stream — real-time task completion events via SSE |
| Readiness | GET /ready — checks PostgreSQL + Redis + Temporal, returns 200/503 |
| Shadow | shadow mode testing endpoints |
| Automation | automated workflow triggers |
| Quotas | tenant resource quotas |
| Metrics | Prometheus /metrics endpoint |
| Integrations | external system connections |
| Health | GET /health (liveness), GET /ready (readiness with dependency checks) |

---

## Tests

| Category | Count | Details |
|----------|-------|---------|
| Sanitizer unit tests | 44 | Input sanitization patterns |
| Prefilter unit tests | 78 | AST allowlist enforcement |
| Normalizer unit tests | 19 | Field mapping validation |
| Red team patch tests | 46 | Template injection, code injection, LOLBin detection, Unicode normalization, classification evasion, field padding |
| Synthetic login tests | 7 | Healer synthetic login check (502/503/401/500/connection refused) |
| Misc unit tests | 14 | Other unit tests |
| Integration tests | 14 | End-to-end pipeline validation |
| Alert corpus | 515 | Full alert corpus for regression testing |
| AutoResearch corpus | 240 | 10 attack types x 24 alerts (12 attack + 12 benign) |
| **Total** | **365 unit + 14 integration + 755-alert corpus** | |

---

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/deploy.sh` | Production deployment |
| `scripts/hardware_check.sh` | Verify hardware requirements |
| `scripts/validate_update.sh` | Full validation after updates |
| `scripts/validate_update_quick.sh` | Quick validation after updates |
| `scripts/build_bundle.sh` | Build air-gap deployment bundle |
| `scripts/flush_code_cache.sh` | Clear Redis code cache (run after prompt changes) |
| `scripts/run_ci_tests.sh` | Run CI test suite |
| `scripts/extract_template_from_investigation.py` | CLI wrapper to extract skill template from a completed investigation |
| `autoresearch/seed_alerts.sh` | Seed 10 diverse alerts (8 attack + 2 benign), wait for completion, print results |
| `autoresearch/cycle10/verify_all.sh` | 15-alert pipeline regression (10 attack + 5 benign), verdict + risk assertions |
| `autoresearch/cycle10/dedup_stress_test.sh` | 14-test dedup adversarial validation (6 categories) |
| `autoresearch/telemetry_driven/run.sh` | Telemetry-driven AutoResearch cycle (collect → analyze → generate → run → score) |
| `cmd/zvadmin/zvadmin.exe` | Host-side admin CLI: diagnose, alerts, model check, dedup health, troubleshoot, update |

---

## Benchmarks

| Benchmark | Result |
|-----------|--------|
| 1000-alert corpus | 983/1000 completed, 100% attack detection, 0 false negatives |
| Juice Shop (100 real-traffic) | 99/100 accuracy (70/70 attacks, 29/30 benign) |
| 200-benign calibration | 200/200 benign, 0% false positive rate |
| Path C novel attacks (10 types) | 10/10 correct (kerberoasting, golden_ticket, LOLBins, etc.) |
| Template fast-fill throughput | ~350ms per investigation |

---

## Coding Conventions

- **Tenant isolation:** Every DB query MUST include `tenant_id` in WHERE clause. Write transactions use `beginTenantTx()` (Go) or `SET LOCAL app.current_tenant` (Python) for RLS.
- **RLS note:** Table owner (`zovark`) bypasses RLS. For production, use `zovark_app` user (already created). RLS is defense-in-depth alongside WHERE clauses.
- **SET LOCAL through PgBouncer:** Use string format `f"SET LOCAL app.current_tenant = '{tenant_id}'"` (not parameterized `$1`) because PgBouncer transaction pooling doesn't support parameterized SET.
- **Error handling (Go):** Use `respondInternalError()` -- never expose `err.Error()` to clients
- **LLM calls:** Always through `worker/stages/llm_gateway.py` via `ZOVARK_LLM_ENDPOINT`
- **No litellm:** Direct httpx POST to llama-server. Zero AI proxy libraries.
- **Model routing:** Use `MODEL_FAST` for param fill, `MODEL_CODE` for code gen + assess
- **Sandbox code:** Must pass AST prefilter allowlist -- only the 16 approved stdlib modules
- **Skill templates:** Stored in `agent_skills.code_template`, use `{{siem_event_json}}` placeholder
- **New activities:** Add to `worker/stages/*.py`, register in `worker/stages/register.py`
- **Migrations:** Sequential in `migrations/`, apply via `docker compose exec -T postgres psql -U zovark -d zovark < migrations/NNN_name.sql`
- **After Python changes:** `docker compose build worker && docker compose up -d worker`
- **After Go changes:** `docker compose build api && docker compose up -d api`
- **Before benchmarks:** Terminate stale Temporal workflows first
- **investigation_memory table:** Name is SINGULAR. Plural reference silently fails.

---

## Contributor Quickstart

Three bash scripts codify the fork → branch → ship → sync loop:

```bash
# Start a fresh feature branch from upstream/master
scripts/git_fresh.sh fix/my-thing

# ... edit files ...

# Commit, push, and print the PR compare URL
# (runs go build + python3 -m py_compile on *changed* files only)
scripts/git_ship.sh 'fix: my-thing description'

# Pull in latest upstream changes (rebase + --force-with-lease push)
scripts/git_sync.sh
```

All three are strict (`set -euo pipefail`), idempotent, and share a
`--help` / `--no-color` / `--quiet` flag contract plus exit codes
`0` = success, `1` = failure, `2` = no-op / degraded. Safety guarantees:
`--force-with-lease` never `--force`, never pushes to `upstream`, never uses
`--no-verify`. Bypass precommit with `ZOVARK_SKIP_PRECOMMIT=1`.

---

## How to Run

```bash
# Start all services (9 core containers)
docker compose up -d

# Start LLM inference (llama-server with Nemotron-Mini-4B)
docker compose -f docker-compose.yml -f docker-compose.distroless.yml up -d
# Wait ~60s for model load, verify: docker compose exec worker curl -sf http://zovark-inference:8080/health

# Verify the whole stack in one shot (API, dashboard, Signoz+trace ingestion,
# healer, Redpanda, Valkey, Temporal, Postgres). Colored table + exit codes.
REDIS_PASSWORD=hydra-redis-dev-2026 scripts/stack_healthcheck.sh
# Or machine-readable: scripts/stack_healthcheck.sh --json

# End-to-end pipeline probe — submits ONE synthetic alert and tracks it
# through ingest → Redpanda → worker → Postgres → Signoz → verdict. Prints a
# per-stage timeline with the stall point named on failure. Reserved task type
# `probe_noop` runs a deterministic no-LLM plan so the probe never burns tokens.
# Writes one tagged synthetic row to agent_tasks (dashboard hides it).
scripts/e2e_probe.sh
# Or chained onto the healthcheck:
scripts/stack_healthcheck.sh --e2e

# Fine-grained probes (covered by stack_healthcheck.sh above):
curl -s http://localhost:8090/health
curl -s http://localhost:8090/ready

# Login (30-minute JWT)
TOKEN=$(curl -s -X POST http://localhost:8090/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@test.local","password":"TestPass2026"}' \
  | sed 's/.*"token":"\([^"]*\)".*/\1/')

# Submit investigation
curl -s -X POST http://localhost:8090/api/v1/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"task_type":"brute_force","input":{"prompt":"SSH brute force","severity":"high","siem_event":{"title":"SSH BF","source_ip":"185.220.101.45","username":"root","rule_name":"BruteForce","raw_log":"500 failed for root from 185.220.101.45"}}}'

# Poll for result
curl -s http://localhost:8090/api/v1/tasks/<TASK_ID> -H "Authorization: Bearer $TOKEN"
```

---

## Key Docs

| Doc | Path |
|-----|------|
| Implementation Audit | `docs/ZOVARK_IMPLEMENTATION_AUDIT.md` |
| Architecture | `docs/ARCHITECTURE.md` |
| API Spec (OpenAPI) | `docs/openapi.yaml` |
| Whitepaper | `docs/WHITEPAPER.md` |
| Sandbox Security | `docs/SANDBOX_SECURITY.md` |
| SIEM Integration | `docs/SIEM_INTEGRATION.md` |
| Juice Shop Benchmark | `docs/JUICE_SHOP_BENCHMARK.md` |
| Model Deployment | `docs/MODEL_DEPLOYMENT.md` |
| Hardware Requirements | `docs/HARDWARE_REQUIREMENTS.md` |
| BlackHat CFP | `docs/outreach/blackhat_cfp.md` |
| CISO Brief | `marketing/outreach/ZOVARK_CISO_Brief.pdf` |
| License Compliance | `LICENSES/README.md` |

---

## Sprints Shipped

| Sprint | What |
|--------|------|
| 1E | Production hardening -- sync commit, SCRAM auth, audit events, FK constraints |
| 1F | Observability -- Prometheus + Grafana monitoring stack |
| 1H | Bootstrap pipeline -- MITRE ATT&CK + CISA KEV ingestion |
| 1I | Model tiering -- prompt versioning + performance tracking |
| 1J | Autoscaling -- KEDA ScaledObject + queue depth exporter |
| 1K | Cross-tenant entity resolution with privacy-preserving hashes |
| 2A | Self-generating detection engine -- Sigma rule generator |
| 2B | SOAR response playbooks -- 5 defaults, approval gates, rollback |
| 2C | Cipher audit skill -- NIST SP 800-57 deterministic + LLM narration |
| 2D | Two-model routing -- American models only (Meta Llama), Qwen removed |
| 2E | Dual LLM routing -- CPU (3B) + GPU (8B), zero swap latency |
| 2F | Security hardening -- input sanitizer, allowlist AST prefilter |
| 2G | Code cache -- Redis-based, repeat patterns skip LLM |
| **3A** | **Wartime sprint -- 10 missions: Docker socket proxy, promotion quorum, RLS, request tracing, fail-closed degradation, flight data recorder, CMMC compliance engine, healer async, SSE dashboard, template CLI** |
| **3B** | **AutoResearch -- autonomous red team (152 experiments, 144 bypasses found) + autonomous template engineer (10/10 templates approved, all fitness >0.98)** |
| **3C** | **Red team patches v1+v2 -- 25 sanitizer patterns, 54 content scanner patterns, IOC provenance, suppression detection, Unicode normalization, tail scanning** |
| **3D** | **Readiness probes + connectivity checks -- GET /ready, healer connectivity monitoring, Docker healthchecks, nginx DNS resolver fix** |
| **3E** | **v3.1-hardening -- Pydantic Settings (SecretStr, .env), LLM output validation (schemas.py), singleton LLM client (Semaphore(2)), streaming waterfall (events.py → SSE → React), Signoz observability (ClickHouse-backed), Code Graph RAG MCP** |
| **3F** | **Healer v1.1 -- ZOVARK_ env prefix alignment, Signoz health checks (OTEL-gated), check_tcp(), LLM_HOST configurable, seed_alerts.sh** |
| **3G** | **v3.2.1 -- Pipeline calibration (kerberoasting/dns_exfil), investigation-aware dedup, batch severity promotion, SIEM verdict push-back, Valkey swap, license compliance, zvadmin telemetry CLI (5 commands), telemetry-driven AutoResearch engine** |

---

## AutoResearch (Autonomous Red Team + Template Engineering)

Lab-only autonomous experimentation loops. Nothing enters production without human review.

### Red Team (`autoresearch/redteam/`)

| Metric | Result |
|--------|--------|
| Total experiments | 152 (v1: 65, v2: 87) |
| Bypasses found | 144 (score >= 3) |
| Vulnerability classes | 6: classification evasion, template injection, code injection, IOC fabrication, risk suppression, field padding |
| All 6 classes patched | Yes -- sanitizer, content scanner, provenance validation, suppression detection, Unicode normalization, tail scanning |
| Files | `program.md`, `payloads.py` (mutable), `evaluate.py` (immutable harness), `validate_bypasses.py` (LLM validation) |

### Template Engineer (`autoresearch/templates/`)

| Metric | Result |
|--------|--------|
| Templates approved | 10/10 (all fitness >= 0.98) |
| Attack types covered | kerberoasting, golden_ticket, dcsync, dll_sideloading, lolbin_abuse, process_injection, wmi_lateral, rdp_tunneling, dns_exfiltration, powershell_obfuscation |
| Accuracy | 100% across all types |
| Speed | 25-36ms avg (vs 120s for Path C) |
| Holdout validation | All passed |
| Files | `program.md`, `candidate.py` (mutable), `evaluate.py` (immutable harness), `setup_test_alerts.py`, `test_alerts.json` (240 alerts) |

---

## Observability

- **OpenTelemetry tracing**: Signoz backend (self-hosted, ClickHouse-backed, air-gap safe)
- **Start**: `docker compose --profile tracing up -d`
- **Signoz UI**: http://localhost:3301 — login: admin@zovark.local / TestPass2026
- **Disable**: `OTEL_ENABLED=false` (pipeline works without tracing)
- **Traces show**: per-stage latency, per-tool execution, LLM call timing, governance decisions
- **Config files**: `config/signoz/` (ClickHouse cluster, OTEL collector, frontend nginx)
- **Schema migration (automated)**: `zovark-signoz-schema-sync` in `docker-compose.yml`
  runs `signoz/signoz-schema-migrator sync` on first boot. The collector has
  `depends_on: zovark-signoz-schema-sync: service_completed_successfully` so it
  doesn't start until ClickHouse has the tables. **No manual migrator run is required**
  (the instruction used to say otherwise — corrected by `telemetry-audit-fix`).
- **Architecture reference**: `docs/TELEMETRY_ARCHITECTURE.md` has a file:line anchor
  table for every wiring touchpoint (API → OTLP → collector → ClickHouse → Signoz UI).
  Re-audit on every OTel SDK upgrade.
- **Verify end-to-end**: `scripts/stack_healthcheck.sh --prime` (generates a warmup
  span and confirms both `zovark-api` and `zovark-worker` are reporting traces).
- **Streaming Waterfall**: real-time tool progress via PostgreSQL NOTIFY → SSE → React component

## MCP Servers for Development

### Signoz MCP (observability queries)
Clone: `git clone https://github.com/DrDroidLab/signoz-mcp-server.git` then `cd signoz-mcp-server && uv sync && uv pip install -e .`
Register: `claude mcp add-json signoz '{"command":"uv","args":["run","python","-m","signoz_mcp_server.mcp_server","-t","stdio"],"env":{"SIGNOZ_HOST":"http://localhost:3301","SIGNOZ_API_KEY":"dummy","SIGNOZ_SSL_VERIFY":"false"},"cwd":"/path/to/signoz-mcp-server"}'`
Requires: Signoz tracing stack running (`docker compose --profile tracing up -d`)
Signoz login: admin@zovark.local / TestPass2026

### Code Graph RAG MCP (codebase knowledge graph)
Uses **vitali87/code-graph-rag** (Memgraph + `uv run … mcp-server`), not the npm `@er77` package.

1. Start dedicated Memgraph: `docker compose -f docker-compose.code-graph-rag.yml up -d` (Bolt on **127.0.0.1:7688**).
2. Cursor `~/.cursor/mcp.json`: server **`code-graph-rag-zovark`** with `TARGET_REPO_PATH` = absolute path to this repo, `MEMGRAPH_HOST=127.0.0.1`, `MEMGRAPH_PORT=7688`, plus `CYPHER_*` for NL→Cypher.
3. Optional: repo-root **`.cgrignore`** excludes bulky trees from the index.

**Troubleshooting (index looks wrong / mixed projects / bogus call edges):**
- **`delete_project` leaves orphans:** Upstream `index_repository` only deletes nodes under `Project` via `CONTAINS_*`. **`Module` and `ExternalPackage` nodes stay** (~400+), so the graph rots after each MCP reindex. Fix: set **`CGR_FULL_GRAPH_RESET_ON_INDEX=true`** in MCP env (patched local `code-graph-rag` supports this) or run **`./scripts/code_graph_rag_reindex.sh`** / `code-graph-rag start --update-graph --clean` for a full wipe.
- **Memgraph must be up** on `MEMGRAPH_PORT` (e.g. `docker compose -f docker-compose.code-graph-rag.yml up -d`).
- **Go / TypeScript not in graph:** The indexer only parses languages with installed **tree-sitter** wheels. This repo’s `code-graph-rag` `pyproject.toml` includes Go/JS/TS/Rust/Java/Cpp/Scala/Lua in main dependencies; run **`uv sync`** in the `code-graph-rag` clone after pull. Verify with: `uv run python -c "from codebase_rag.parser_loader import load_parsers; print(load_parsers()[0].keys())"` — must include `go` and `typescript`.
- **Semantic embeddings:** Optional extras are not installed by default; logs may say *“Semantic search dependencies not available”* — structural graph still works.
- **Some NL queries fail** with Memgraph *“Not yet implemented”* — simplify Cypher or upgrade Memgraph; not an indexing failure.
- **Multi-statement Cypher from the LLM** is truncated to the first statement before execution (Memgraph one-query-per-call).

Claude Code CLI equivalent: `claude mcp add --transport stdio code-graph-rag-zovark --env TARGET_REPO_PATH="$(pwd)" --env MEMGRAPH_HOST=127.0.0.1 --env MEMGRAPH_PORT=7688 … -- uv run --directory /path/to/code-graph-rag code-graph-rag mcp-server`
Index: Use `batch_index` tool in Claude Code after first setup, continue until 100%
Query: Ask natural language questions about the codebase
```

---

## Known Issues

1. **Healer HTTP thread** — Blocks during health check cycles on Windows Docker Desktop (GIL + subprocess contention). Async fix applied but Windows GIL issue persists. Works on Linux.
2. **DB/Redis passwords** — Still `hydra_dev_2026` / `hydra-redis-dev-2026`. Intentional, non-breaking.
3. **DPO pipeline** — Training data exists in `dpo/` but no production model trained.
4. **SIEM lab Filebeat** — Needs polling mode + bind mount on Windows Docker.
5. **RLS owner bypass** — `zovark` user owns tables and bypasses RLS. Use `zovark_app` user in production for enforcement. Already created with GRANT permissions.
6. **3 pre-existing test failures** — `test_adversarial_review.py` TestReviewFailSafe (3 tests). The adversarial review passes through when LLM unavailable; tests expect blocking. Not a security issue.
7. **Nginx localhost IPv6** — Alpine resolves `localhost` to `::1` but nginx binds `0.0.0.0`. All healthchecks use `127.0.0.1` explicitly.
8. **Valkey RDB migration** — Switching from Redis 7 to Valkey 7 requires clearing the Redis data volume (`docker volume rm zovark_redis_data`) because RDB format v12 is incompatible. Dedup counters and code cache are transient — no data loss.
9. **Mock Ollama in test stack** — `docker-compose.test.yml` uses `mock-ollama` container for CI. This is intentional (test fixture), not a production dependency.
10. **Gemma 4 E4B requires >=12GB Docker memory** — Q4_K_M (5GB GGUF) needs ~7GB RAM with default 128K context. Fixed with `--ctx-size 4096` (reduces to ~2GB). Docker Desktop must be set to >=12GB for reliable operation. Uses `--jinja --reasoning off`. GBNF grammars verified working.
11. **Healer memory leak** — On Windows Docker Desktop, healer can grow to 3GB+ with 5000+ PIDs (GIL + asyncio contention). Memory-limited to 512MB in docker-compose.yml. Restart healer if it hits the limit.
12. **pgx + PgBouncer prepared statements** — Default `ZOVARK_PGX_QUERY_MODE=describe_exec`. Do not change without reading `docs/RUNBOOK_HEALTHCHECK.md#api-08p01`. The API's startup self-test (`api/db.go` `selfTestPool`) refuses to boot if the live pool is in a mode that triggers SQLSTATE `08P01` against PgBouncer transaction pooling.
13. **Schema drift on dev volumes** — Migrations 054+ are not applied by docker-compose. Run `scripts/apply_migrations.sh` after a fresh `docker compose down -v && up -d`. The API logs `schema_migrations_check status=absent` if the ledger is missing. See `docs/RUNBOOK_HEALTHCHECK.md#schema-drift`. Migration 068 (SurrealDB cutover) is gated behind `--include-068`.

---

## What Was Built This Session (March 29-30, 2026)

From commit 8507c11 to f0f8b2f — 27 commits in one session:

1. **Complete HYDRA→Zovark rebrand** — 100+ files, all code/config/docs/monitoring/MCP server
2. **Two-model routing** — Meta Llama 3.2 3B + 3.1 8B, zero Chinese dependencies
3. **3 Llama calibration fixes** — prose stripping, risk anchors, verdict override
4. **Dual LLM routing** — CPU (3B) + GPU (8B) endpoints, zero swap latency
5. **Security hardening** — input sanitizer (12 patterns), allowlist AST prefilter, smart_truncate
6. **Redis code cache** — repeat patterns skip LLM (24h TTL)
7. **CI/CD layer** — mock LLM server + 14 integration tests + GitHub Actions
8. **SIEM lab** — Juice Shop → Elastic → Poller → Bridge → Zovark (end-to-end verified)
9. **Template promotion flywheel** — Path C → analyst review → auto-promote → Path A (428ms)
10. **SOC War Room dashboard** — new design system, 2 new pages, 5 new components, Zovark logo
11. **Zovark Core normalizer** — 70+ field mappings, 4 SIEM formats
12. **TaskList data fix** — verdict/risk/path columns show real data from API
13. **JWT extended** to 30 minutes
14. **Template-only mode** — `ZOVARK_MODE=templates-only` for $40K Essentials tier
15. **Smart batching** — 60% alert reduction, severity-aware windows
16. **Circuit breaker** — GREEN/YELLOW/RED auto-degradation during storms
17. **Plain-English summaries** — deterministic bullet-point for L1 analysts
18. **Hardware check script** — validates deployment hardware, recommends tier
19. **DMZ deployment script** — one-command `deploy.sh` with SIEM webhook config
20. **VM appliance template** — Packer for Ubuntu 24.04 OVA/QCOW2
21. **Fleet Agent self-healer** — AI crash diagnosis, 3-level escalation, Sneakernet UI
22. **Crypto bundle system** — Ed25519 signed .zvk update packages
23. **Parallel worker pool** — 8 concurrent activities via Temporal (was 1)
24. **smart_truncate** — fixes 10K padding truncation vulnerability
25. **61 demo investigations** loaded (32 benign + 29 attacks, zero FP/FN)

## What Was Built This Session (March 30-31, 2026)

### Wartime Sprint — 10 Missions
1. **Docker socket proxy** — Worker no longer mounts Docker socket directly. Proxy allows only container lifecycle ops. Images/exec/networks/volumes blocked (403).
2. **Template promotion quorum** — 2-person approval required. `template_promotion_approvals` table. `/promotion-approve` endpoint. Dashboard shows approval status.
3. **PostgreSQL RLS** — `current_tenant_id()` function + policies on 10 tables. `beginTenantTx()` helper. `zovark_app` user created for production enforcement.
4. **Request tracing** — UUID `trace_id` generated at task creation, stored in `agent_tasks` + `audit_events`, returned in `X-Zovark-Trace-ID` header.
5. **Fail-safe degradation** — LLM fail-closed (Path C → `needs_manual_review`). Redis failover (process duplicates vs miss alerts). Circuit breaker integration.
6. **Flight data recorder** — `GET /admin/diagnostics/export` returns `.zvk` zip with audit events, LLM logs, healer status, system info. Secrets scrubbed by 5 regex patterns.
7. **CMMC compliance engine** — `POST /compliance/report/cmmc` maps IR.L2-3.6.1/2/3 controls to Zovark audit events with evidence counts and MITRE coverage.
8. **Healer async** — Health checks run concurrently via `asyncio.gather`. Synthetic login check auto-restarts dashboard on 502.
9. **SSE dashboard** — `GET /tasks/stream` global SSE endpoint. PostgreSQL `NOTIFY task_completed` from store.py. Polling fallback.
10. **Template extraction CLI** — `scripts/extract_template_from_investigation.py` wraps existing `template_promoter.py`.

### AutoResearch — Autonomous Experimentation
11. **Red team v1** — 65 experiments, 60 bypasses. Found 6 vulnerability classes.
12. **Red team v2** — 87 experiments, 84 bypasses. Found 15 uncovered LOLBins, Unicode homoglyph bypass, provenance manipulation.
13. **Red team patches** — 25 sanitizer patterns (was 12), 54 content scanner patterns, IOC provenance validation, suppression detection, Cyrillic homoglyph map, zero-width char stripping, tail scanning. 46 new tests.
14. **Template engineer** — 10/10 attack types approved (all fitness >0.98, 100% accuracy, 25-36ms). Imported to `agent_skills`.
15. **Quorum flow verified** — `api_key_abuse` alert → Path C → analyst review → 2-person approval → Path A template.

### Operational Fixes
16. **Nginx DNS cache** — Added `resolver 127.0.0.11 valid=5s` to dashboard nginx config. Prevents 502 after API container recreation.
17. **Redis URL parsing** — Fixed `initRedis()` to use `redis.ParseURL()` for `redis://` URLs (was silently failing).
18. **Readiness probe** — `GET /ready` checks PostgreSQL + Redis + Temporal. Docker healthchecks use readiness (not just liveness).
19. **Connectivity checks** — Healer monitors API→DB, worker→LLM, dashboard→API connectivity every 60s.
20. **Dashboard verdict fix** — PromotionQueue sent `analyst_verdict: "confirmed"` but API only accepts `true_positive/false_positive/suspicious/benign`. Fixed.

## What Was Built (March 31, 2026) — v3 Tool-Calling Migration

### Phase 1: Tool Library (34 tools)
1. **8 extraction tools** — IPv4, IPv6, domains, URLs, hashes, emails, usernames, CVEs with evidence_refs
2. **4 analysis tools** — pattern counting, Shannon entropy, encoding detection, base64 decode
3. **5 parsing tools** — Windows events, syslog, auth logs, DNS queries, HTTP requests
4. **6 scoring tools** — brute force, phishing, lateral movement, exfiltration, C2 beacon, generic
5. **7 detection tools** — kerberoasting, golden ticket, ransomware, phishing, C2, data exfil, LOLBin
6. **4 enrichment tools** — MITRE ATT&CK mapping, known-bad lookup, cross-investigation correlation, institutional knowledge
7. **AutoResearch harness** — evaluate.py with 11 test case types, 0.95 fitness threshold

### Phase 2: Tool Runner + Plans
8. **Tool runner** — variable resolution ($raw_log, $siem_event.field, $stepN), conditional branching, timeouts, error isolation, IOC dedup
9. **24 investigation plans** — all attack types + benign routing, saved in investigation_plans.json
10. **DB migration 062** — governance_config, institutional_knowledge, agent_skills.investigation_plan

### Phase 3: Pipeline Rewrite + Governance
11. **analyze.py v3 path** — saved plan loading, investigation_plans.json fallback, LLM tool selection, institutional knowledge injection
12. **execute.py v3 path** — in-process tool runner, no Docker sandbox
13. **govern.py** — autonomy slider (observe/assist/autonomous) between assess and store
14. **investigation_workflow.py** — v3/v2 branching, governance activity, plan pass-through

### Phase 5: Red Team v3
15. **21 security tests** — tool argument injection, variable resolution injection, plan manipulation, conditional bypass, enrichment safety
16. **0 critical vulnerabilities** found

### Phase 6: Cleanup
17. **docs/V3_MIGRATION_REPORT.md** — full migration report with architecture comparison
18. **CLAUDE.md updated** — v3 architecture, tool catalog, governance, feature flag

## What Was Built — v3.1-hardening Sprint (March 31, 2026)

### Phase 1: Merge + Tag
1. **v3.0.0 tagged on master** — redteam branch merged, release tag created

### Phase 2: Pydantic Settings
2. **worker/settings.py** — ZovarkSettings with ZOVARK_ env prefix, SecretStr for db_password/redis_password, database_url/redis_url properties, .env file support
3. **All 7 pipeline stages updated** — ingest, analyze, execute, assess, govern, store, llm_gateway read from settings instead of hardcoded defaults

### Phase 3: LLM Output Validation
4. **worker/schemas.py** — VerdictOutput (verdict enum, risk 0-100, MITRE regex filter), IOCItem (hash length validation, CVE format), ToolSelectionOutput (catalog check). Invalid data → safe fallback, never crash.
5. **17 new tests** — valid/invalid verdicts, risk range, MITRE filtering, hash validation, CVE format, tool selection

### Phase 4: LLM Client Singleton
6. **worker/llm_client.py** — Singleton httpx.AsyncClient, asyncio.Semaphore(2) max concurrent LLM calls, OTEL span per request, 120s read timeout. Path A bypasses semaphore entirely.

### Phase 5: Streaming Investigation Waterfall
7. **worker/events.py** — PostgreSQL NOTIFY on `investigation_events` channel. Events: tool_started, tool_completed, ioc_discovered, mitre_mapped, verdict_ready. Human-readable tool summaries. Fire-and-forget.
8. **api/sse.go** — Added LISTEN investigation_events alongside task_completed. Events forwarded with event type and trace_id.
9. **Dashboard SSE** — `TaskList.tsx` / `TaskDetail.tsx` subscribe to `GET /api/v1/tasks/stream` via `EventSource` (token query param); demo pipeline UI uses `InvestigationWaterfall` and related components on `/demo`.

### Phase 6: Code Graph RAG + Signoz MCP
10. **Code Graph RAG MCP** — registered for codebase knowledge graph queries
11. **Signoz MCP** — DrDroidLab server registered for trace/metric queries from Claude Code

### Phase 7: Signoz Fix
12. **config/signoz/frontend-nginx.conf** — Fixed nginx proxy: `location = /api` (exact) → `location /api` (prefix). Signup/login now work.
13. **Signoz credentials** — admin@zovark.local / TestPass2026

## What Was Built — v3.1 Healer + AutoResearch Prep (March 31, 2026)

### Healer v1.1
1. **ZOVARK_ env prefix alignment** — Healer reads `ZOVARK_DB_USER`, `ZOVARK_DB_PASSWORD`, `ZOVARK_REDIS_PASSWORD`, `ZOVARK_LLM_BASE_URL`, `ZOVARK_LLM_FAST_MODEL`, `ZOVARK_OTEL_ENABLED` (falls back to old names). docker-compose.yml updated to pass these.
2. **Signoz health checks** — 4 new checks gated on `OTEL_ENABLED`: ClickHouse TCP 9000, OTEL Collector TCP 4318, Signoz Query HTTP `/api/v1/health`, Signoz Frontend HTTP `:3301`. When OTEL disabled, containers discovered but checks return OK immediately.
3. **check_tcp()** — New TCP connect health check function for native protocol ports.
4. **LLM_HOST configurable** — Was hardcoded `http://host.docker.internal:11434`, now reads `ZOVARK_LLM_BASE_URL` env var.
5. **No Jaeger refs** — Confirmed none existed in healer (already removed in prior commit 81a29ab).
6. **API port verified** — Confirmed healer checks API on port 8090 (correct).

### AutoResearch Prep
7. **autoresearch/seed_alerts.sh** — Seeds 10 diverse investigations (8 attack types + 2 benign), waits 5 minutes, prints results from DB. Handles MinGW path conversion (`MSYS_NO_PATHCONV=1`) and Windows Defender AV evasion for LOLBin payloads.
8. **Stale data cleanup procedure** — FK-safe deletion order: `template_promotion_approvals` → `analyst_feedback` → `entity_edges` → `entities` → `investigation_memory` → `audit_events` → `agent_tasks`. Must also terminate stale Temporal workflows and flush Redis.
9. **10/10 seed investigations verified** — All completed with correct verdicts: 8 attacks (true_positive, risk 70-100), 2 benign (benign, risk 0).

### Operational Notes
- **Before clearing data for benchmarks:** Must terminate all open Temporal workflows first, or stale workflows will block the worker. Use: `tctl --address zovark-temporal:7233 workflow listall --op` then terminate each.
- **agent_tasks schema:** Results are in `output` JSONB column (not `result`). Access via `output->>'verdict'`, `output->>'risk_score'`.
- **MinGW/Git Bash on Windows:** Set `MSYS_NO_PATHCONV=1` in scripts that pass URLs or Windows-style paths to curl. Windows Defender may block curl payloads containing `certutil.exe -urlcache` patterns.

## What Was Built — Continuous AutoResearch Cycles (April 1, 2026)

### Cycle 1: Telemetry-Driven AutoResearch System v1.0 (edbb6a9)
1. **telemetry_reader.py** — Queries 7 data sources (Signoz, PostgreSQL, Temporal, LLM inference, Code Graph RAG, test coverage, red team status)
2. **Phase 0 telemetry collection** — Automated at cycle start, produces priority queue
3. **Track 1: Critical latency fix** — Enabled `ZOVARK_FAST_FILL=true`, stage.assess: 24.3s → 0.026s (99.9% improvement)
4. **Track 2: +10 red team vectors** — Timing/encoding themed (Slowloris padding, Unicode homoglyphs, nested encoding, etc.)
5. **autoresearch/continuous/** — Directory structure for cycle tracking, scoreboard.json, cycle plans
6. **20 total vectors** — All with `investigation_plan` field

### Cycle 2: System Verification + Red Team Expansion (311f858)
1. **Telemetry verification** — FAST_FILL optimization confirmed persistent (0.026s p95)
2. **Track 2: +10 vectors** — Advanced persistence theme (WMI events, COM hijacking, DLL sideloading, BITS jobs, etc.)
3. **Total vectors: 20** (10 Cycle 1 + 10 Cycle 2)
4. **System health**: 100% detection, 0% FP, 0 errors

### Cycle 3: Bypass Fixes + evaluate.py Operational (98875bd)
1. **evaluate.py DEBT PAID** — Fixed path issues, now runs successfully against live API
2. **4 complete bypasses fixed**:
   - `detect_com_hijacking` — COM hijacking via registry (was 0, now 85)
   - `detect_encoded_service` — Base64 encoded PowerShell in services (was 0, now 85)
   - `detect_token_impersonation` — RunAs with saved creds (was 0, now 100)
   - `detect_appcert_dlls` — AppCert DLLs persistence (was 0, now 100)
3. **Tool catalog**: 11 detection tools (+4)
4. **Investigation plans**: Updated `privilege_escalation_hunt` and `lateral_movement_detection` with new tools
5. **Evaluate.py results**: 13/20 full detection (65%), 0 complete bypasses (0%)
6. **MITRE mappings**: T1546.015 (COM), T1546.009 (AppCert), T1134.001 (token), T1543.003 (service)

### Cycle 4+: Ongoing
Tracks 3-6 (templates, tool hardening, benchmarks, tests) now operational with debt cleared.

---

## What Was Built — Cycle 8: Burst Protection + Pipeline Fixes (April 2, 2026)

### 3-Layer Pre-Temporal Alert Funnel
1. **Layer 1: Investigation-Aware Dedup** (`api/alert_dedup.go`) — v2 JSON entries with task_id/status/verdict/severity. SHA-256 hash of 6 canonical fields. Severity escalation bypass (higher severity → new workflow). Failed investigation retry. `force_reinvestigate` analyst override. `dedup_count` column on agent_tasks. TTL by severity (15min-2hrs). Fail-open.
2. **Layer 2: Batch Buffer** (`api/batch_buffer.go`) — Groups by (task_type, source_ip) in 5s window via atomic Redis Lua script. Severity promotion: higher-severity alerts replace batch representative. Uses original batch window for expiry. 10 same-IP alerts → 1 workflow. Severity multipliers (critical=1.25s, info=15s). Fail-open.
3. **Layer 3: Backpressure** (`api/backpressure.go`) — Redis sorted set tracks workflow starts. Soft limit (200) → queue for drain goroutine. Hard limit (1000) → HTTP 503 + Retry-After. Drain goroutine processes 10 queued tasks every 2 seconds.
4. **Integration** — All 3 layers added to `createTaskHandler()` (`task_handlers.go`) and `createIngestTask()` (`siem_ingest.go`). Covers all 4 alert entry points.
5. **Drain goroutine** — Started in `api/main.go` on server boot.

### Pipeline Fixes
6. **Plan alias resolution** (`worker/stages/analyze.py`) — 20 aliases mapping SIEM task_types to investigation_plans.json keys (e.g., `phishing` → `phishing_investigation`, `ransomware` → `ransomware_triage`). Plus substring matching fallback.
7. **MITRE field fix** (`worker/stages/assess.py`) — Pydantic VerdictOutput now checks both `technique_id` and `id` fields from MITRE dicts.
8. **Worker scaling** (`docker-compose.yml`) — `MAX_CONCURRENT_WORKFLOWS` 16→32, `MAX_CONCURRENT_ACTIVITIES` 8→16.

### Testing & Documentation
9. **100-alert API smoke test** (`scripts/smoke_test_100.sh`) — 70 attack + 30 benign through full pipeline. 62/62 attacks detected (100%), 0 false negatives.
10. **HANDOVER.md** — AI-to-AI handover guide with testing protocol, architecture constraints, anti-patterns. Prevents next AI from testing tools in isolation.
11. **END_TO_END_WORKFLOW.md** — Complete flow trace from HTTP request through 10 middleware layers, 3 pre-Temporal filters, 6 pipeline stages, SSE streaming, to React dashboard.

### Key New Environment Variables
| Variable | Default | Purpose |
|----------|---------|---------|
| `ZOVARK_API_DEDUP_ENABLED` | `true` | Layer 1 pre-Temporal dedup |
| `ZOVARK_API_BATCH_ENABLED` | `true` | Layer 2 batch buffer |
| `ZOVARK_API_BATCH_WINDOW_SECONDS` | `5` | Batch grouping window |
| `ZOVARK_MAX_PENDING_WORKFLOWS` | `200` | Backpressure soft limit |
| `ZOVARK_MAX_PENDING_WORKFLOWS_HARD` | `1000` | Backpressure hard limit |
| `ZOVARK_BACKPRESSURE_ENABLED` | `true` | Layer 3 backpressure |

---

## What Was Built — v3.2.1 Fix Sprint (April 3, 2026)

### Pipeline Calibration (Cycle 10)
1. **Output validator v3 fix** (`output_validator.py`) — Empty findings valid when `tools_executed` or `plan_executed` present. Prevents benign FPs from safe_default risk=50.
2. **Kerberoasting recalibrated** (`detection.py`) — RC4+TGS+non-krbtgt: 55→80. RC4+TGS+krbtgt: 25→35. RC4-only: 30→45.
3. **New detect_dns_exfiltration tool** (`detection.py`, `catalog.py`) — High-entropy subdomains, TXT abuse, volume detection. Registered in catalog (tool #40). dns_exfiltration plan updated.
4. **Risk floor lowered** (`assess.py`) — Attack risk floor 36→25. `_derive_verdict` suspicious threshold 36→25.
5. **Cycle 10 verification** — 15/15 (10 attacks, 5 benign). All attacks ≥65, all benign ≤25.

### Critical Infrastructure
6. **System tenant** (`migrations/063_system_tenant.sql`) — UUID `00000000-0000-0000-0000-000000000001` for break-glass auth. RLS-compatible.
7. **OOB readiness channel** (`api/oob.go`, `api/main.go`) — `net.Listen` + channel signal before main server starts. Guarantees OOB accepting connections first.

### Investigation-Aware Dedup (v2)
8. **Structured dedup entries** (`api/alert_dedup.go`) — v2 JSON in Redis with task_id/status/verdict/severity. Backward compatible (v1 strings treated as duplicates).
9. **Severity escalation bypass** — Higher severity alerts create new investigation instead of being suppressed.
10. **Failed retry** — Errored/failed investigations don't permanently suppress future alerts.
11. **Force reinvestigate** — `force_reinvestigate: true` in task input bypasses all dedup layers.
12. **Dedup counter** (`migrations/064_dedup_count.sql`) — `dedup_count` column on agent_tasks. Incremented on each dedup hit.
13. **Dedup observability** — Redis counters (new_alert/deduplicated/severity_escalation/retry) in OOB `/debug/state`.
14. **Store writes back** (`worker/stages/store.py`) — Updates Redis dedup entry with verdict/risk on completion.
15. **Batch severity promotion** (`api/batch_buffer.go`) — Lua script tracks severity, promotes representative. Uses original batch window for expiry.
16. **Dedup stress test** — 14 tests, 6 categories. 13 pass, 0 fail, 1 skip (LLM timeout).

### SIEM Verdict Push-Back
17. **Push-back engine** (`api/siem_pushback.go`) — POST verdicts to Splunk HEC, Elastic, or generic webhook. 2 retries, 10s timeout. Fire-and-forget via goroutine.
18. **Triggered from SSE** (`api/sse.go`) — `triggerPushbackFromNotify()` on `task_completed` NOTIFY.
19. **Config via system_configs** — `siem.pushback.enabled`, `siem.pushback.type` (splunk/elastic/webhook), `siem.pushback.url`, `siem.pushback.token`.

### Valkey Swap
20. **Redis → Valkey** (`docker-compose.yml`) — `redis:7-alpine` → `valkey/valkey:7-alpine` (BSD license). Zero code changes. Cleared RDB volume for compatibility.

### License Compliance
21. **LICENSES/ directory** — 10 files: 7 third-party license texts (Valkey BSD, Temporal MIT, Gin MIT, Pydantic MIT, PostgreSQL, Signoz MIT, Docker Apache 2.0), Llama 3 Community License, model attribution, README.

### zvadmin Telemetry CLI
22. **Shared telemetry layer** (`cmd/zvadmin/telemetry.go`) — OOB HTTP, PostgreSQL via docker exec, Redis via docker exec, Docker inspect, nvidia-smi.
23. **zvadmin diagnose** — 8 checks (services, throughput, dedup, model/GPU, database, queue, containers, disk). Exit 0/1/2. `--json` flag.
24. **zvadmin alerts** — Pipeline stats with verdict bar chart, top types, low-confidence alerts, latency by path.
25. **zvadmin model check** — Per-type risk calibration report, attack/benign separation gap, MITRE coverage, flagged types.
26. **zvadmin dedup health** — Decision distribution with bars, efficiency rating, top deduped rules, TTL status.
27. **zvadmin troubleshoot** — 5 symptoms (alerts-stuck, slow-dashboard, wrong-verdicts, high-resources, post-reboot). Interactive menu. Stops at first root cause.
28. **zvadmin update** — Staging directory, backup, rollback on unhealthy/benchmark failure.

### Telemetry-Driven AutoResearch Engine
29. **6-module Python engine** (`autoresearch/telemetry_driven/`) — collector (PG+Redis+OOB), analyzer (8 weakness checks), generator (targeted alerts from weaknesses), runner (API submission+polling), delta analyzer (improvement/regression detection), cycle orchestrator.
30. **Host wrapper** (`run.sh`) — Copies modules to worker container, runs cycle, copies results back.
31. **Validated** — Full cycle: 25 weaknesses found, 15 tests generated, 100% detection, 0% FP.

### Ollama De-coupling
32. **User-facing strings cleaned** — All zvadmin output, API health handler, healer connectivity checks use "inference" / "LLM inference" instead of "Ollama".
33. **Health checks generalized** — API reads from `ZOVARK_LLM_ENDPOINT` env vars. zvadmin tries `/health` (llama.cpp) then `/api/tags` (Ollama compat).
34. **Functional references preserved** — `restartHostLLM()` tries zovark-inference first, ollama as fallback. `preload_llm_models()` with backward compat alias.

### Key New Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `siem.pushback.enabled` | `false` | Enable SIEM verdict push-back (system_configs) |
| `siem.pushback.type` | -- | splunk, elastic, or webhook |
| `siem.pushback.url` | -- | Target URL for verdict POST |
| `siem.pushback.token` | -- | Auth token (is_secret=true) |

---

## Pending Work

1. **Merge v3.1-hardening to master** — All v3.1 + v3.2.1 work is on v3.1-hardening branch
2. **Build distroless inference container** — llama.cpp in `docker-compose.distroless.yml`, test on Linux with GPU
3. **Build web-admin** — `cd web-admin && npm install && npm run build`
4. **A100 benchmark** — Rerun with parallel workers on fast hardware
5. **Healthcare template pack** — 30 industry-specific templates (10 done via AutoResearch)
6. **Blue/green deployment** — Zero-downtime updates with auto-rollback
7. **Community template sync** — Network effect moat across customers
8. **Public self-serve demo** — Standalone browser demo for CISO outreach
9. **Design partner outreach** — Target healthcare MSSPs first
10. **Switch to zovark_app DB user** — Enable RLS enforcement in production
