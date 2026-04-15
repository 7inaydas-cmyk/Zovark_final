# Zovark v3.2.1 — AI-to-AI Handover Guide

> Read this BEFORE reading CLAUDE.md. Read this BEFORE writing any code.
> This is the "how to work here" guide. CLAUDE.md is the "what exists" reference.
> If they conflict, HANDOVER.md wins for process; CLAUDE.md wins for architecture.

---

## 1. What Zovark Is (30-second version)

Air-gapped SOC investigation platform. Receives SIEM alerts, runs deterministic tool-based investigations, delivers structured verdicts with risk scores, IOCs, and MITRE ATT&CK mappings. Targets regulated enterprises (CMMC/HIPAA/GDPR). Runs entirely on-premise with no cloud dependencies.

**The pipeline is the product. Individual tools mean nothing in isolation.**

```
Alert → API (:8090) → Temporal → Ingest → Analyze → Execute → Assess → Govern → Store → Verdict
```

**Stack:** Go API + Python Temporal Worker + React Dashboard + PostgreSQL/pgvector + Valkey + llama-server (Nemotron-Mini-4B)

---

## 2. Architecture Invariants (MANDATORY — DO NOT VIOLATE)

Every prior session that violated one of these caused a regression that took hours to fix.

### 2.1 Test Through the API Only

**NEVER** test pipeline changes by calling Python functions directly. The pipeline has 6 stages with middleware between each. A tool that works in isolation will produce different results than the full pipeline because ingest sanitizes input, the tool runner resolves variables, assess applies signal boost and provenance validation, and the output validator checks schema conformance.

```bash
# Login once, reuse the token
TOKEN=$(curl -sf -X POST http://localhost:8090/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@test.local","password":"TestPass2026"}' \
  | grep -o '"token":"[^"]*"' | cut -d'"' -f4)

# Submit via API, wait 60-120s, then poll
curl -sf -X POST http://localhost:8090/api/v1/tasks \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"task_type":"brute_force","input":{"prompt":"SSH brute force","severity":"high","siem_event":{...}}}'
```

### 2.2 Two-Model Architecture

Zovark uses TWO LLM roles — same model on dev, different on customer tier:

| Role | Pipeline Stage | Env Var | Dev Model | Customer Model |
|------|---------------|---------|-----------|----------------|
| **FAST** | Tool selection, param fill | `ZOVARK_MODEL_FAST` | Gemma 4 E4B | Same |
| **CODE** | Verdict assessment, summary | `ZOVARK_MODEL_CODE` | Gemma 4 E4B | Bigger (8B-70B) |

Do NOT collapse FAST and CODE into a single variable. Do NOT hardcode model names.

### 2.3 Inference is llama-server, NOT Ollama

- Engine: llama-server (llama.cpp) in container `zovark-inference` on port **8080**
- Endpoint: `http://zovark-inference:8080/v1/chat/completions`
- Start: `docker compose -f docker-compose.yml -f docker-compose.distroless.yml up -d`
- Model: `models/gemma-4-e4b-it-Q4_K_M.gguf` (5.0GB, `--ctx-size 4096 --jinja --reasoning off`)
- Previous: `models/Nemotron-Mini-4B-Instruct-Q4_K_M.gguf` (2.6GB, kept for rollback)
- **No Ollama. No port 11434. No host.docker.internal.** All references were purged 2026-04-04.

### 2.4 Tenant Isolation is Mandatory

Every DB query MUST include `tenant_id`. Write transactions use:
- Go: `beginTenantTx(ctx, tenantID)`
- Python: `SET LOCAL app.current_tenant = '{tenant_id}'` (string format — PgBouncer requirement)

### 2.5 Fail-Closed on LLM Down

When the LLM is unavailable: Path A (saved plans) + benign routing continue normally. Path C alerts get `verdict=needs_manual_review` — NEVER benign. Circuit breaker goes RED.

### 2.6 Signal Boost Scans SIEM Data Only

`assess.py` signal boost scans `raw_log + title + rule_name` — NOT tool execution stdout. Tool stdout contains JSON keys and variable references that trigger false positive regex matches. This was a major Cycle 9 bug.

---

## 3. How to Start the System

```bash
# 0. Populate .env — REQUIRED (stabilize-runtime-hygiene, 2026-04-15)
#    docker-compose.yml and Pydantic settings both fail-fast on missing creds.
#    `docker compose up -d` with an empty env will exit non-zero and name the
#    first missing variable. Copy .env.example and fill in every placeholder.
cp .env.example .env
# ...edit .env, replace every REPLACE_ME_* value...

# 1. Start core services
docker compose up -d

# 2. Start inference (llama-server + Gemma 4 E4B)
docker compose -f docker-compose.yml -f docker-compose.distroless.yml up -d zovark-inference

# 3. Wait ~60s for model load, then verify
docker compose exec worker curl -sf http://zovark-inference:8080/health
curl -sf http://localhost:8090/ready
cmd/zvadmin/zvadmin.exe diagnose    # Full 8-check diagnostic
```

### Credentials

> **stabilize-runtime-hygiene (2026-04-15):** dev credentials below are the
> long-standing non-rotated defaults that must be set explicitly in `.env` —
> they are no longer provided as compose fallbacks. Production deployments
> MUST override every value via a secrets manager.

| Resource | Credential |
|----------|------------|
| Admin login | admin@test.local / TestPass2026 (tenant e1c1bc5d) |
| Analyst login | analyst2@test.local / TestPass2026 |
| Database | user=zovark, password=hydra_dev_2026, db=zovark |
| Redis/Valkey | password=hydra-redis-dev-2026 |
| LLM endpoint | http://zovark-inference:8080/v1/chat/completions |

---

## 4. How to Verify Changes

### Quick Check (2 min)
```bash
docker compose build worker && docker compose up -d worker && sleep 30
cmd/zvadmin/zvadmin.exe diagnose
```

### Pipeline Regression (4 min)
```bash
bash autoresearch/cycle10/verify_all.sh
# EXPECT: 15/15 (10 attacks ≥65 risk, 5 benign ≤25 risk)
```

### Dedup Stress Test (7 min)
```bash
bash autoresearch/cycle10/dedup_stress_test.sh
# EXPECT: 13-14/14 passed, 0 failed
```

### AutoResearch Cycle (4 min)
```bash
bash autoresearch/telemetry_driven/run.sh --hours 24 --max-tests 15 --wait 120
# Collects real telemetry, generates targeted tests, measures improvement
```

### Expected Verdicts

| task_type | Expected Verdict | Expected Risk |
|-----------|-----------------|---------------|
| brute_force | true_positive | 70-100 |
| phishing | true_positive | 70-100 |
| ransomware | true_positive | 80-100 |
| kerberoasting | true_positive | 65-100 |
| dns_exfiltration | true_positive | 65-100 |
| c2_communication | true_positive | 70-100 |
| password_change | benign | 0-15 |
| windows_update | benign | 0 |
| health_check | benign | 0 |

---

## 5. Anti-Patterns (Mistakes That Were Made Before)

| Mistake | Why It's Wrong | What to Do Instead |
|---------|---------------|-------------------|
| Calling `detect_phishing()` directly | Bypasses plan orchestration and assess scoring | Submit via API with `task_type=phishing` |
| Scanning tool stdout in signal boost | JSON keys trigger false positive attack regex matches | Signal boost scans SIEM data only (raw_log, title, rule_name) |
| Empty findings → safe_default(risk=50) | In v3 tools mode, empty findings is valid (nothing suspicious found) | Check `tools_executed`/`plan_executed` before rejecting |
| Hardcoding `host.docker.internal:11434` | Stale Ollama reference, breaks in container | Read from `ZOVARK_LLM_ENDPOINT` env var |
| Risk floor at 36 | Too high, causes benign FPs on attack-typed task_types | Floor is 25 — low enough to not override tool judgment |
| Running 100 tests with 100 logins | Triggers rate limiter (10/15min) | Login once, reuse token |
| Using `python3` on the host | No Python on this Windows machine | Use `docker compose exec -T worker python` |
| Modifying investigation_workflow.py | Breaks Temporal state machine | Modify stages, not the orchestrator |
| Self-verifying output | Confirmation bias masks errors | Use multi-agent verification |
| Adding llama-server flags without GBNF test | Can silently disable grammar constraints | Test grammar isolation before benchmark |
| Assuming model fits in Docker memory | Gemma 4 E4B needed 7GB, VM had 5.8GB — crash loop | Check `docker info` total memory before model swaps |

---

## 6. Key File Map

### Engineering Framework
| File | What |
|------|------|
| `ENGINEERING_DISCIPLINE.md` | Claude Code operating framework, slash commands, anti-patterns |

### Pipeline Stages
| File | Stage | LLM? |
|------|-------|------|
| `worker/stages/ingest.py` | 1. Sanitize, normalize, dedup, route | No |
| `worker/stages/analyze.py` | 2. Load plan (A) or LLM tool select (C) | FAST |
| `worker/stages/execute.py` | 3. Run tool plan with variables | No |
| `worker/stages/assess.py` | 4. Verdict, signal boost, IOC provenance | CODE |
| `worker/stages/govern.py` | 4.5. Autonomy slider | No |
| `worker/stages/store.py` | 5. DB write, NOTIFY, dedup update | No |

### LLM Infrastructure
| File | What |
|------|------|
| `worker/stages/llm_gateway.py` | Dual endpoint routing, model swap, audit logging |
| `worker/llm_client.py` | Singleton httpx, dual semaphores (FAST/CODE), grammar support, OTEL |
| `worker/grammars/tool_selection.gbnf` | Grammar-constrained decoding for tool selection JSON |
| `worker/grammars/verdict.gbnf` | Grammar-constrained decoding for verdict JSON |
| `worker/tools/tool_subsets.py` | Pruned catalogs per attack type (~60% fewer tokens) |
| `dpo/prompts_v2.py` | Full prompt library: system, task, tools, scoring anchors (~900 LOC) |

### Burst Protection (3 layers)
| File | Layer |
|------|-------|
| `api/alert_dedup.go` | L1: Investigation-aware Redis dedup (v2 JSON, severity escalation, retry) |
| `api/batch_buffer.go` | L2: Lua-atomic batch buffer with severity promotion |
| `api/backpressure.go` | L3: Workflow queue depth throttle + drain goroutine |

### Calibration (when risk scores are wrong)
| File | What Controls |
|------|--------------|
| `worker/stages/assess.py` :463-483 | Signal boost patterns (+45 per match on SIEM data) |
| `worker/stages/assess.py` :577 | Risk floor (attack-typed at risk 25-69 → boost to 70) |
| `worker/stages/assess.py` :38-60 | `_derive_verdict()` risk→verdict thresholds |
| `worker/tools/detection.py` | Per-tool risk weights |
| `dpo/prompts_v2.py` :168-191 | Scoring anchors in LLM prompt |

### Host-Side CLI
| Command | What |
|---------|------|
| `zvadmin diagnose` | 8-check health diagnostic with operator actions |
| `zvadmin alerts --hours 24` | Pipeline stats, verdict bar chart, low-confidence list |
| `zvadmin model check` | Risk calibration report, attack/benign separation gap |
| `zvadmin dedup health` | Dedup decision distribution, efficiency rating |
| `zvadmin troubleshoot --symptom post-reboot` | Guided troubleshooting (5 symptoms) |

---

## 7. Current State (2026-04-04)

### Working
- 15/15 pipeline regression on Gemma 4 E4B via llama-server (swapped from Nemotron 2026-04-04)
- 13/14 dedup stress test (1 skip = LLM timeout)
- Investigation-aware dedup with severity escalation, force reinvestigate
- SIEM verdict push-back (Splunk HEC + Elastic + webhook)
- Valkey 7.2 (BSD, replaced Redis)
- zvadmin CLI (11 commands)
- Telemetry-driven AutoResearch engine
- Inference optimization (prefix caching, tool pruning, GBNF grammars, dual semaphores)
- ALL Ollama references purged from active code and docs

### Not Yet Done
1. Merge v3.1-hardening to master
2. Build web-admin (`cd web-admin && npm install && npm run build`)
3. GPU inference (Dockerfile.inference currently CPU-only from source)
4. A100 benchmark
5. Switch to zovark_app DB user for RLS enforcement

### Known Calibration Gaps
- `privilege_escalation_hunt` stddev=49.8 (inconsistent scoring)
- MITRE coverage 0% for several attack types (not propagating from map_mitre tool to output)

---

## 8. Rebuild Commands

```bash
# Python changes (worker/stages/tools)
docker compose build worker && docker compose up -d worker

# Go API changes
docker compose build api && docker compose up -d api

# zvadmin changes (Windows)
MSYS_NO_PATHCONV=1 docker run --rm -v "$(pwd)/cmd/zvadmin:/build" -w /build \
  golang:1.22-alpine sh -c "apk add --no-cache git && CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -o zvadmin.exe ."

# Inference container
docker compose -f docker-compose.yml -f docker-compose.distroless.yml build zovark-inference
docker compose -f docker-compose.yml -f docker-compose.distroless.yml up -d zovark-inference

# After prompt changes
scripts/flush_code_cache.sh

# New migration (next: 065)
docker compose exec -T postgres psql -U zovark -d zovark < migrations/065_name.sql
```

---

## 9. Environment Variables That Matter

| Variable | Default | What It Controls |
|----------|---------|-----------------|
| `ZOVARK_EXECUTION_MODE` | `tools` | `tools` = v3 (default). `sandbox` = v2 legacy. |
| `ZOVARK_LLM_ENDPOINT` | `http://zovark-inference:8080/v1/chat/completions` | Where LLM calls go |
| `ZOVARK_LLM_ENDPOINT_FAST` | same | FAST role endpoint |
| `ZOVARK_LLM_ENDPOINT_CODE` | same | CODE role endpoint (customer: separate container) |
| `ZOVARK_MODEL_FAST` | `gemma-4-e4b-it` | FAST model alias |
| `ZOVARK_MODEL_CODE` | `gemma-4-e4b-it` | CODE model alias (customer: bigger) |
| `ZOVARK_FAST_FILL` | `false` | `true` = skip LLM, template-only |
| `ZOVARK_MODE` | `full` | `templates-only` = no LLM fallback |
| `ZOVARK_GPU_LAYERS` | `0` | GPU layers for llama-server (99 for full GPU offload) |

---

## 10. Database Patterns

```bash
# Read-only (safe)
docker compose exec -T postgres psql -U zovark -d zovark -c "SELECT ..."

# FK-safe deletion order (for clearing test data):
# template_promotion_approvals → analyst_feedback → entity_edges → entities
# → investigation_memory → audit_events → agent_tasks

# Before clearing data: terminate stale Temporal workflows first
# tctl --address zovark-temporal:7233 workflow listall --op

# agent_tasks results are in the `output` JSONB column (not `result`)
# Access: output->>'verdict', output->>'risk_score', (output->>'risk_score')::int
```

---

## 11. Unit Tests

```bash
# All tests (inside worker container)
docker compose exec -T worker python -m pytest tests/ -q --tb=short

# Known pre-existing failures (skip these):
# - test_adversarial_review.py::TestReviewFailSafe (3 tests, LLM-dependent)

# After changes, also run the API-level regression:
bash autoresearch/cycle10/verify_all.sh    # 15 alerts through full pipeline
```
