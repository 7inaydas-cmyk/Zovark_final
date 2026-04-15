#!/usr/bin/env bash
# =============================================================================
# Zovark End-to-End Pipeline Probe
#
# Submits ONE synthetic SIEM alert to the running stack and tracks it through
# every pipeline stage, printing a per-hop timeline and naming the stall point
# on failure.
#
# Stages (each measured independently):
#   0.   setup            — generate probe id, login, capture Signoz baselines
#   0.5. db_write         — POST /api/v1/admin/diagnostics/probe-db sanity write before ingest
#   0.6. schema_ledger    — verify schema_migrations matches migrations/*.sql on disk
#   1.   ingest           — POST /api/v1/tasks with task_type=probe_noop
#   2. redpanda           — verify the message landed in tasks.new.<tenant>
#   3. pg.investigating   — wait for status transition out of pending/queued
#   4. pg.completed       — wait for terminal status
#   5. signoz             — verify span-count delta for zovark-api + zovark-worker
#   6. verdict            — verify non-null verdict in agent_tasks.output
#   7. cleanup            — tag the probe row with _probe_cleanup=true
#
# Exit codes:
#   0  every stage passed end-to-end
#   1  one or more stages failed (STALL line names the hop)
#   2  every stage passed but at least one is degraded (e.g. trace delta partial)
#
# Required tooling: bash, jq, curl, docker CLI, and Compose-managed containers
# for postgres + redpanda. No Python, no LLM dependency.
#
# Safety: each run writes ONE row to agent_tasks with input.synthetic=true +
# input._probe_cleanup=true. Dashboard filters hide those rows. Operator
# cleanup query (documented in docs/RUNBOOK_HEALTHCHECK.md):
#   DELETE FROM agent_tasks
#    WHERE (input->>'synthetic')::boolean
#      AND created_at < NOW() - INTERVAL '30 days';
# =============================================================================

set -euo pipefail
MSYS_NO_PATHCONV=1

# ---------------------------------------------------------------------- Guards
if ! command -v jq >/dev/null 2>&1; then
    echo "ERROR: jq is required (apt: jq / brew: jq)" >&2
    exit 2
fi
if ! command -v curl >/dev/null 2>&1; then
    echo "ERROR: curl is required" >&2
    exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker CLI is required" >&2
    exit 2
fi

# ---------------------------------------------------------------------- Defaults
ZOVARK_API_BASE="${ZOVARK_API_BASE:-http://127.0.0.1:8090}"
ZOVARK_SIGNOZ_BASE="${ZOVARK_SIGNOZ_BASE:-http://127.0.0.1:3301}"
ZOVARK_PROBE_EMAIL="${ZOVARK_PROBE_EMAIL:-admin@test.local}"
ZOVARK_PROBE_PASSWORD="${ZOVARK_PROBE_PASSWORD:-TestPass2026}"
ZOVARK_PROBE_TASK_TYPE="${ZOVARK_PROBE_TASK_TYPE:-probe_noop}"
ZOVARK_REDIS_PASSWORD="${ZOVARK_REDIS_PASSWORD:-}"
# database-seed-system change: fallback tenant_id when the login response
# doesn't include one. Points at the dev tenant seeded by
# migrations/seed_dev_data.sql. Primary path is still the login response.
ZOVARK_PROBE_TENANT_ID="${ZOVARK_PROBE_TENANT_ID:-00000000-0000-0000-0000-000000000010}"

OUTPUT_JSON=false
NO_COLOR=false
TOTAL_TIMEOUT=120
TENANT_OVERRIDE=""
SIGNOZ_REQUIRED=true
SKIP_CLEANUP=false
CHECK_FIXTURE_ONLY=false

# Per-stage timeouts (seconds)
STAGE3_TIMEOUT=90   # waiting for pg transition out of pending/queued
STAGE4_TIMEOUT=60   # waiting for terminal state
SIGNOZ_POLL_TIMEOUT=10

# ---------------------------------------------------------------------- CLI
usage() {
    cat <<'EOF'
Usage: scripts/e2e_probe.sh [OPTIONS]

Submit one synthetic alert and track it through every pipeline stage.

OPTIONS:
  --json                   Emit a single JSON object (implies --no-color).
  --no-color               Suppress ANSI color escapes.
  --timeout N              Total wall-clock budget in seconds (default 120).
  --tenant ID              Override the tenant_id derived from the login response.
  --signoz-required BOOL   true|false (default true). When false, a Signoz
                           failure at stage 5 downgrades to `degraded` instead
                           of `fail`.
  --skip-cleanup           Leave the probe row untouched (debug helper).
  --check-fixture          Verify the admin fixture user can log in (runs
                           only Stage 0 setup then exits 0 on success). Use
                           this as a fast "is the seed applied?" gate without
                           burning the full probe budget.
  --help                   Show this help and exit 0.

ENV VARS:
  ZOVARK_API_BASE          default http://127.0.0.1:8090
  ZOVARK_SIGNOZ_BASE       default http://127.0.0.1:3301
  ZOVARK_PROBE_EMAIL       default admin@test.local
  ZOVARK_PROBE_PASSWORD    default TestPass2026
  ZOVARK_PROBE_TASK_TYPE   default probe_noop
  ZOVARK_REDIS_PASSWORD    optional
  ZOVARK_PROBE_TENANT_ID   default 00000000-0000-0000-0000-000000000010
                           (fallback when login response omits tenant_id)

EXIT CODES:
  0   every stage passed end-to-end
  1   one or more stages failed; STALL line names the stage
  2   every stage passed but at least one is degraded

SIDE EFFECTS:
  Writes ONE row to agent_tasks with input.synthetic=true + input._probe_cleanup=true.
  Safe to run repeatedly. 30-day cleanup query documented in docs/RUNBOOK_HEALTHCHECK.md.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --json) OUTPUT_JSON=true; NO_COLOR=true; shift ;;
        --no-color) NO_COLOR=true; shift ;;
        --timeout)
            [ $# -lt 2 ] && { echo "ERROR: --timeout requires a value" >&2; exit 2; }
            TOTAL_TIMEOUT="$2"; shift 2 ;;
        --timeout=*) TOTAL_TIMEOUT="${1#--timeout=}"; shift ;;
        --tenant)
            [ $# -lt 2 ] && { echo "ERROR: --tenant requires a value" >&2; exit 2; }
            TENANT_OVERRIDE="$2"; shift 2 ;;
        --signoz-required)
            [ $# -lt 2 ] && { echo "ERROR: --signoz-required requires true|false" >&2; exit 2; }
            case "$2" in true|false) SIGNOZ_REQUIRED="$2"; shift 2 ;;
                         *) echo "ERROR: --signoz-required takes true|false" >&2; exit 2 ;; esac ;;
        --skip-cleanup) SKIP_CLEANUP=true; shift ;;
        --check-fixture) CHECK_FIXTURE_ONLY=true; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "ERROR: unknown flag: $1" >&2; usage >&2; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------- Colors
if [ "$NO_COLOR" = false ] && [ -t 1 ]; then
    C_RESET=$'\033[0m'
    C_BOLD=$'\033[1m'
    C_GREEN=$'\033[32m'
    C_RED=$'\033[31m'
    C_YELLOW=$'\033[33m'
    C_GRAY=$'\033[90m'
    C_CYAN=$'\033[36m'
else
    C_RESET=""; C_BOLD=""; C_GREEN=""; C_RED=""; C_YELLOW=""; C_GRAY=""; C_CYAN=""
fi

_status_color() {
    case "$1" in
        pass) printf '%s' "$C_GREEN" ;;
        fail) printf '%s' "$C_RED" ;;
        degraded) printf '%s' "$C_YELLOW" ;;
        skip) printf '%s' "$C_GRAY" ;;
        *) printf '%s' "$C_RESET" ;;
    esac
}

# ---------------------------------------------------------------------- Helpers
_now_ms() {
    local d
    d=$(date +%s%3N 2>/dev/null || true)
    if [[ "$d" =~ ^[0-9]+$ ]] && [ "$d" != "0" ]; then
        printf '%s' "$d"
        return
    fi
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import time; print(int(time.time()*1000))'
        return
    fi
    printf '%d' "$(( $(date +%s) * 1000 ))"
}

# Container discovery: inlined fallback so this script stands alone.
# Caches a service -> container_name map at first call.
declare -A COMPOSE_MAP=()
COMPOSE_MAP_LOADED=false

_discover_containers() {
    [ "$COMPOSE_MAP_LOADED" = true ] && return 0
    COMPOSE_MAP_LOADED=true

    local raw
    # docker compose ps --format json may emit a JSON array OR newline-delimited
    # JSON objects depending on the compose CLI version. `jq --slurp 'flatten'`
    # normalises both.
    if raw=$(docker compose ps --format json 2>/dev/null) && [ -n "$raw" ]; then
        while IFS=$'\t' read -r svc name; do
            [ -z "$svc" ] && continue
            COMPOSE_MAP[$svc]="$name"
        done < <(printf '%s' "$raw" | jq -rs '
            map(if type == "array" then .[] else . end)
            | .[]
            | [(.Service // .service // ""), (.Name // .name // "")]
            | @tsv
        ' 2>/dev/null || true)
    fi
}

# Known alias table for the docker-ps fallback when compose discovery misses.
_alias_for() {
    case "$1" in
        postgres) echo "zovark-postgres" ;;
        redis|valkey) echo "zovark-redis" ;;
        temporal) echo "zovark-temporal" ;;
        redpanda) echo "zovark-redpanda" ;;
        api) echo "zovark-api" ;;
        worker) echo "zovark-worker-1" ;;
        healer) echo "zovark-healer" ;;
        dashboard) echo "zovark-dashboard" ;;
        signoz) echo "zovark-signoz" ;;
        signoz-collector) echo "zovark-signoz-collector" ;;
        *) echo "" ;;
    esac
}

_resolve_container() {
    local service="$1"
    _discover_containers
    local name="${COMPOSE_MAP[$service]:-}"
    if [ -n "$name" ]; then
        printf '%s' "$name"
        return 0
    fi
    # Fallback: docker ps --filter by known alias
    local alias
    alias=$(_alias_for "$service")
    if [ -z "$alias" ]; then
        return 1
    fi
    name=$(docker ps --filter "name=^${alias}$" --format '{{.Names}}' 2>/dev/null | head -1)
    if [ -z "$name" ]; then
        # Prefix match as last resort (covers scaled containers like zovark-worker-1)
        name=$(docker ps --filter "name=^${alias}" --format '{{.Names}}' 2>/dev/null | head -1)
    fi
    if [ -n "$name" ]; then
        printf '%s' "$name"
        return 0
    fi
    return 1
}

_pg_exec() {
    local sql="$1"
    local container
    if ! container=$(_resolve_container postgres); then
        return 2
    fi
    docker exec -i "$container" psql -U zovark -d zovark -t -A -v ON_ERROR_STOP=1 -c "$sql" 2>&1
}

_rpk_tail() {
    local topic="$1" num="$2"
    local container
    if ! container=$(_resolve_container redpanda); then
        return 2
    fi
    docker exec -i "$container" rpk topic consume "$topic" --offset end --num "$num" --format json 2>&1
}

# ---------------------------------------------------------------------- Stage state
# Parallel arrays storing one row per stage.
STAGES_NAMES=()
STAGES_STATUS=()
STAGES_AT_MS=()       # absolute monotonic timestamp
STAGES_HOP_MS=()      # time since previous stage's AT
STAGES_DETAIL=()

PROBE_START_MS=0
LAST_STAGE_MS=0
STALL_STAGE=""
TASK_ID=""
TENANT_ID=""
ACCESS_TOKEN=""
PROBE_ID=""
SIGNOZ_API_BASELINE=0
SIGNOZ_WORKER_BASELINE=0

record_stage() {
    local name="$1" status="$2" detail="$3"
    local now hop
    now=$(_now_ms)
    if [ "$LAST_STAGE_MS" -eq 0 ]; then
        hop=0
    else
        hop=$(( now - LAST_STAGE_MS ))
    fi
    STAGES_NAMES+=("$name")
    STAGES_STATUS+=("$status")
    STAGES_AT_MS+=("$now")
    STAGES_HOP_MS+=("$hop")
    STAGES_DETAIL+=("$detail")
    LAST_STAGE_MS=$now

    if [ "$status" = "fail" ] && [ -z "$STALL_STAGE" ]; then
        STALL_STAGE="$name"
    fi
}

# ---------------------------------------------------------------------- Timeout guard
_exceeded_total_timeout() {
    local now
    now=$(_now_ms)
    local elapsed_s=$(( (now - PROBE_START_MS) / 1000 ))
    [ "$elapsed_s" -gt "$TOTAL_TIMEOUT" ]
}

# =============================================================================
# Stage 0 — setup
# =============================================================================
stage0_setup() {
    # Generate probe id.
    if command -v uuidgen >/dev/null 2>&1; then
        PROBE_ID=$(uuidgen | tr '[:upper:]' '[:lower:]')
    else
        PROBE_ID=$(python3 -c 'import uuid; print(uuid.uuid4())' 2>/dev/null || echo "")
    fi
    if [ -z "$PROBE_ID" ]; then
        record_stage "0 setup" "fail" "could not generate probe_id"
        return 1
    fi

    # Login to obtain Bearer token + tenant_id.
    local login_body login_resp
    login_body=$(jq -cn --arg e "$ZOVARK_PROBE_EMAIL" --arg p "$ZOVARK_PROBE_PASSWORD" \
        '{email:$e, password:$p}')
    if ! login_resp=$(curl -fsS --max-time 10 \
            -X POST "$ZOVARK_API_BASE/api/v1/auth/login" \
            -H "Content-Type: application/json" \
            -d "$login_body" 2>&1); then
        record_stage "0 setup" "fail" "login http failed: $(echo "$login_resp" | head -c 120)"
        return 1
    fi
    ACCESS_TOKEN=$(echo "$login_resp" | jq -r '.token // empty' 2>/dev/null || true)
    if [ -z "$ACCESS_TOKEN" ] || [ ${#ACCESS_TOKEN} -lt 20 ]; then
        record_stage "0 setup" "fail" "login response missing token"
        return 1
    fi
    if [ -n "$TENANT_OVERRIDE" ]; then
        TENANT_ID="$TENANT_OVERRIDE"
    else
        TENANT_ID=$(echo "$login_resp" | jq -r '.user.tenant_id // .tenant_id // empty' 2>/dev/null || true)
    fi
    # database-seed-system: fall back to the seeded dev tenant when the login
    # response omits tenant_id (e.g., transient response-shape drift).
    if [ -z "$TENANT_ID" ]; then
        TENANT_ID="$ZOVARK_PROBE_TENANT_ID"
    fi
    if [ -z "$TENANT_ID" ]; then
        record_stage "0 setup" "fail" "could not resolve tenant_id from login or fallback"
        return 1
    fi

    # Capture Signoz baselines. On failure with --signoz-required=false, set to 0
    # and continue; otherwise mark Stage 0 as a hard fail.
    local services_body end_ms start_ms
    end_ms=$(_now_ms)
    start_ms=$(( end_ms - 10 * 60 * 1000 ))
    if services_body=$(curl -fsS --max-time 5 \
            "$ZOVARK_SIGNOZ_BASE/api/v1/services?start=${start_ms}&end=${end_ms}" 2>&1); then
        SIGNOZ_API_BASELINE=$(echo "$services_body" | jq -r '
            [.data[]?, .services[]?, .[]?]
            | map(select((.serviceName // .name) == "zovark-api"))
            | (.[0].numCalls // .[0].num_calls // 0)
        ' 2>/dev/null || echo 0)
        SIGNOZ_WORKER_BASELINE=$(echo "$services_body" | jq -r '
            [.data[]?, .services[]?, .[]?]
            | map(select((.serviceName // .name) == "zovark-worker"))
            | (.[0].numCalls // .[0].num_calls // 0)
        ' 2>/dev/null || echo 0)
        # Coerce to integer
        [[ "$SIGNOZ_API_BASELINE" =~ ^[0-9]+$ ]] || SIGNOZ_API_BASELINE=0
        [[ "$SIGNOZ_WORKER_BASELINE" =~ ^[0-9]+$ ]] || SIGNOZ_WORKER_BASELINE=0
    else
        if [ "$SIGNOZ_REQUIRED" = "true" ]; then
            record_stage "0 setup" "fail" "signoz unreachable: $(echo "$services_body" | head -c 80)"
            return 1
        fi
        SIGNOZ_API_BASELINE=0
        SIGNOZ_WORKER_BASELINE=0
    fi

    record_stage "0 setup" "pass" \
        "probe_id=${PROBE_ID:0:8}… tenant=${TENANT_ID:0:8}… baselines: api=$SIGNOZ_API_BASELINE worker=$SIGNOZ_WORKER_BASELINE"
    return 0
}

# =============================================================================
# Stage 0.5 — db_write
# Sanity-check that the API can complete a parameterised INSERT...RETURNING +
# DELETE round-trip BEFORE we try to ingest. This catches the pgx<->PgBouncer
# prepared-statement collision (SQLSTATE 08P01) at its true point of failure
# instead of letting it surface as a misleading "ingest stall". See
# docs/RUNBOOK_HEALTHCHECK.md#api-08p01.
# =============================================================================
stage05_db_write() {
    local resp http_code tmp_body
    tmp_body=$(mktemp)
    # shellcheck disable=SC2064
    trap "rm -f '$tmp_body'" RETURN
    http_code=$(curl -sS --max-time 10 \
        -o "$tmp_body" -w '%{http_code}' \
        -X POST "$ZOVARK_API_BASE/api/v1/admin/diagnostics/probe-db" \
        -H "Authorization: Bearer $ACCESS_TOKEN" \
        -H "Content-Type: application/json" 2>&1 || echo "000")
    resp=$(cat "$tmp_body" 2>/dev/null || true)

    if [ "$http_code" = "200" ]; then
        local ok row_id took_ms
        ok=$(echo "$resp" | jq -r '.ok // empty' 2>/dev/null || true)
        row_id=$(echo "$resp" | jq -r '.row_id // empty' 2>/dev/null || true)
        took_ms=$(echo "$resp" | jq -r '.took_ms // empty' 2>/dev/null || true)
        if [ "$ok" = "true" ] && [[ "$row_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
            record_stage "0.5 db_write" "pass" "took_ms=${took_ms} row_id=${row_id:0:8}…"
            return 0
        fi
        record_stage "0.5 db_write" "fail" "http=200 but body shape unexpected: $(echo "$resp" | head -c 120)"
        return 1
    fi

    # Failure path. Look for 08P01 either in the response body or in fresh API
    # logs and emit a one-line operator hint pointing at the pgx env var fix.
    local detail="http=${http_code} body=$(echo "$resp" | head -c 120)"
    local saw_08p01=false
    if echo "$resp" | grep -q '08P01' 2>/dev/null; then
        saw_08p01=true
    elif command -v docker >/dev/null 2>&1 \
         && docker logs zovark-api --since 10s 2>&1 | grep -q '08P01'; then
        saw_08p01=true
    fi
    if $saw_08p01; then
        detail="${detail} 08P01"
        printf 'hint: pgx pool may be in cache_statement mode against PgBouncer — set ZOVARK_PGX_QUERY_MODE=exec; see docs/RUNBOOK_HEALTHCHECK.md#api-08p01\n' >&2
    fi
    record_stage "0.5 db_write" "fail" "$detail"
    return 1
}

# =============================================================================
# Stage 0.6 — schema_ledger
# Verify the schema_migrations ledger is in sync with the migrations/*.sql on
# disk. If any file is on disk but absent from the ledger, the DB is drifted
# and the operator needs to run scripts/apply_migrations.sh. This catches the
# failure mode at its true point of origin instead of letting it surface as a
# misleading "ingest stall" or a column-not-found error mid-pipeline.
# See docs/RUNBOOK_HEALTHCHECK.md#schema-drift.
# =============================================================================
stage06_schema_ledger() {
    local ledger_out ledger_err
    ledger_err=$(mktemp)
    # shellcheck disable=SC2064
    trap "rm -f '$ledger_err'" RETURN

    if ! ledger_out=$(docker exec zovark-postgres psql -U zovark -d zovark -tAc \
            "SELECT filename FROM schema_migrations ORDER BY filename" 2> "$ledger_err"); then
        if grep -q '42P01\|does not exist' "$ledger_err"; then
            record_stage "0.6 schema_ledger" "fail" "ledger absent — run scripts/apply_migrations.sh"
            printf 'hint: schema_migrations table not found — run scripts/apply_migrations.sh; see docs/RUNBOOK_HEALTHCHECK.md#schema-drift\n' >&2
            return 1
        fi
        record_stage "0.6 schema_ledger" "fail" "psql query failed: $(head -c 100 "$ledger_err")"
        return 1
    fi

    # Exclude 068_*.sql from the drift comparison: that migration is the
    # SurrealDB / pgvector retirement and is intentionally gated behind
    # `scripts/apply_migrations.sh --include-068` + an APPLY-068 confirmation.
    # Until the cutover happens, 068 SHOULD be on disk but absent from the
    # ledger — that is the steady state, not drift.
    local on_disk
    on_disk=$(find "$(dirname "$0")/../migrations" -maxdepth 1 -name '*.sql' \
        ! -name 'seed_*' ! -name '068_*' -printf '%f\n' 2>/dev/null | sort)

    local drift
    drift=$(comm -23 <(echo "$on_disk") <(echo "$ledger_out"))

    local applied_count
    applied_count=$(echo "$ledger_out" | wc -l)

    if [ -z "$drift" ]; then
        record_stage "0.6 schema_ledger" "pass" "applied=${applied_count} drift=0"
        return 0
    fi

    local drift_count first_five
    drift_count=$(echo "$drift" | wc -l)
    first_five=$(echo "$drift" | head -5 | paste -sd, -)
    record_stage "0.6 schema_ledger" "fail" "drift: ${first_five}"
    printf 'hint: %d migration(s) on disk but not in ledger — run scripts/apply_migrations.sh; see docs/RUNBOOK_HEALTHCHECK.md#schema-drift\n' "$drift_count" >&2
    return 1
}

# =============================================================================
# Stage 1 — ingest
# =============================================================================
stage1_ingest() {
    local body resp http_code
    body=$(jq -cn \
        --arg tt "$ZOVARK_PROBE_TASK_TYPE" \
        --arg pid "$PROBE_ID" \
        '{
            task_type: $tt,
            input: {
                prompt: "e2e probe",
                severity: "low",
                synthetic: true,
                probe_id: $pid,
                trace_id: $pid,
                siem_event: {
                    title: "e2e probe",
                    raw_log: "probe alert from scripts/e2e_probe.sh probe_id=\($pid)",
                    source_ip: "127.0.0.1",
                    rule_name: "probe_noop",
                    severity: "low"
                }
            }
        }')

    local tmp_body
    tmp_body=$(mktemp)
    # shellcheck disable=SC2064
    trap "rm -f '$tmp_body'" RETURN
    http_code=$(curl -sS --max-time 15 \
        -o "$tmp_body" -w '%{http_code}' \
        -X POST "$ZOVARK_API_BASE/api/v1/tasks" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $ACCESS_TOKEN" \
        -d "$body" 2>&1 || echo "000")
    resp=$(cat "$tmp_body" 2>/dev/null || true)

    if [ "$http_code" != "200" ] && [ "$http_code" != "201" ] && [ "$http_code" != "202" ]; then
        record_stage "1 ingest" "fail" "http=$http_code body=$(echo "$resp" | head -c 120)"
        return 1
    fi
    TASK_ID=$(echo "$resp" | jq -r '.task_id // empty' 2>/dev/null || true)
    if [ -z "$TASK_ID" ]; then
        record_stage "1 ingest" "fail" "http=$http_code but task_id missing"
        return 1
    fi
    record_stage "1 ingest" "pass" "task_id=${TASK_ID:0:8}… http=$http_code"
    return 0
}

# =============================================================================
# Stage 2 — redpanda
# =============================================================================
stage2_redpanda() {
    local topic="tasks.new.${TENANT_ID}"
    local attempt=0
    local max_attempts=3
    local raw found_at=""
    while [ $attempt -lt $max_attempts ]; do
        attempt=$(( attempt + 1 ))
        raw=$(_rpk_tail "$topic" 20 2>&1 || true)
        if [ -z "$raw" ]; then
            sleep 1
            continue
        fi
        # rpk emits newline-delimited JSON. Grep for our task id.
        if printf '%s\n' "$raw" | grep -q "\"task_id\":\"$TASK_ID\""; then
            # Try to pull the offset for the matching line.
            found_at=$(printf '%s\n' "$raw" \
                | grep "\"task_id\":\"$TASK_ID\"" \
                | head -1 \
                | jq -r '.offset // empty' 2>/dev/null || true)
            break
        fi
        sleep 1
    done
    if [ -z "$raw" ]; then
        record_stage "2 redpanda" "fail" "rpk consume produced no output"
        return 1
    fi
    if ! printf '%s\n' "$raw" | grep -q "\"task_id\":\"$TASK_ID\""; then
        record_stage "2 redpanda" "fail" "task_id not found in last 20 messages of $topic after $max_attempts retries"
        return 1
    fi
    if [ -n "$found_at" ]; then
        record_stage "2 redpanda" "pass" "found in $topic @offset $found_at"
    else
        record_stage "2 redpanda" "pass" "found in $topic (offset unknown)"
    fi
    return 0
}

# =============================================================================
# Stage 3 — postgres: wait for transition out of pending/queued
# =============================================================================
stage3_pg_investigating() {
    local deadline_ms
    deadline_ms=$(( $(_now_ms) + STAGE3_TIMEOUT * 1000 ))
    local last_state="unknown"
    while [ "$(_now_ms)" -lt "$deadline_ms" ]; do
        if _exceeded_total_timeout; then
            record_stage "3 pg.investigating" "fail" "total timeout exceeded (last state: $last_state)"
            return 1
        fi
        local state
        state=$(_pg_exec "SELECT status FROM agent_tasks WHERE id = '$TASK_ID'" 2>&1 || echo "")
        state=$(printf '%s' "$state" | tr -d '[:space:]')
        if [ -n "$state" ]; then
            last_state="$state"
            case "$state" in
                pending|queued) : ;;
                *)
                    record_stage "3 pg.investigating" "pass" "state=$state"
                    return 0 ;;
            esac
        fi
        sleep 0.5
    done
    # fix-e2e-ingest-stall §3: enrich the timeout detail with a state-specific
    # operator hint so the next on-call doesn't have to grep the runbook.
    local hint=""
    case "$last_state" in
        pending)
            hint=" — likely consumer cold start; check 'docker compose logs worker | grep redpanda'"
            ;;
        queued)
            hint=" — likely backpressure; check Temporal queue depth and ZOVARK_MAX_PENDING_WORKFLOWS"
            ;;
    esac
    record_stage "3 pg.investigating" "fail" "stuck at '$last_state' for ${STAGE3_TIMEOUT}s${hint}"
    return 1
}

# =============================================================================
# Stage 4 — postgres: wait for terminal state
# =============================================================================
stage4_pg_completed() {
    local deadline_ms
    deadline_ms=$(( $(_now_ms) + STAGE4_TIMEOUT * 1000 ))
    local last_state="unknown"
    while [ "$(_now_ms)" -lt "$deadline_ms" ]; do
        if _exceeded_total_timeout; then
            record_stage "4 pg.completed" "fail" "total timeout exceeded (last state: $last_state)"
            return 1
        fi
        local state
        state=$(_pg_exec "SELECT status FROM agent_tasks WHERE id = '$TASK_ID'" 2>&1 || echo "")
        state=$(printf '%s' "$state" | tr -d '[:space:]')
        if [ -n "$state" ]; then
            last_state="$state"
            case "$state" in
                completed|needs_review|needs_manual_review|needs_analyst_review|failed|error)
                    record_stage "4 pg.completed" "pass" "terminal=$state"
                    return 0 ;;
            esac
        fi
        sleep 0.5
    done
    record_stage "4 pg.completed" "fail" "stuck at '$last_state' for ${STAGE4_TIMEOUT}s"
    return 1
}

# =============================================================================
# Stage 5 — Signoz span-count delta
# =============================================================================
stage5_signoz() {
    local services_body end_ms start_ms api_after worker_after
    end_ms=$(_now_ms)
    start_ms=$(( end_ms - 2 * 60 * 1000 ))
    if ! services_body=$(curl -fsS --max-time "$SIGNOZ_POLL_TIMEOUT" \
            "$ZOVARK_SIGNOZ_BASE/api/v1/services?start=${start_ms}&end=${end_ms}" 2>&1); then
        if [ "$SIGNOZ_REQUIRED" = "true" ]; then
            record_stage "5 signoz" "fail" "signoz unreachable: $(echo "$services_body" | head -c 80)"
            return 1
        fi
        record_stage "5 signoz" "degraded" "signoz unreachable (non-required)"
        return 0
    fi

    api_after=$(echo "$services_body" | jq -r '
        [.data[]?, .services[]?, .[]?]
        | map(select((.serviceName // .name) == "zovark-api"))
        | (.[0].numCalls // .[0].num_calls // 0)
    ' 2>/dev/null || echo 0)
    worker_after=$(echo "$services_body" | jq -r '
        [.data[]?, .services[]?, .[]?]
        | map(select((.serviceName // .name) == "zovark-worker"))
        | (.[0].numCalls // .[0].num_calls // 0)
    ' 2>/dev/null || echo 0)
    [[ "$api_after" =~ ^[0-9]+$ ]] || api_after=0
    [[ "$worker_after" =~ ^[0-9]+$ ]] || worker_after=0

    local api_delta=$(( api_after - SIGNOZ_API_BASELINE ))
    local worker_delta=$(( worker_after - SIGNOZ_WORKER_BASELINE ))
    local detail="zovark-api Δ=$api_delta, zovark-worker Δ=$worker_delta"

    if [ "$api_delta" -ge 1 ] && [ "$worker_delta" -ge 1 ]; then
        record_stage "5 signoz" "pass" "$detail"
        return 0
    fi
    if [ "$api_delta" -ge 1 ] || [ "$worker_delta" -ge 1 ]; then
        local missing="zovark-api"
        [ "$api_delta" -ge 1 ] && missing="zovark-worker"
        record_stage "5 signoz" "degraded" "$detail — missing new spans from $missing"
        return 0
    fi
    # Neither service emitted new spans.
    if [ "$SIGNOZ_REQUIRED" = "true" ]; then
        record_stage "5 signoz" "fail" "$detail — no new spans from either service"
        return 1
    fi
    record_stage "5 signoz" "degraded" "$detail — no new spans (non-required)"
    return 0
}

# =============================================================================
# Stage 6 — verdict
# =============================================================================
stage6_verdict() {
    local row
    row=$(_pg_exec "SELECT COALESCE(output->>'verdict', '') || '|' || COALESCE(output->>'risk_score', '') FROM agent_tasks WHERE id = '$TASK_ID'" 2>&1 || echo "")
    row=$(printf '%s' "$row" | tr -d '\n\r' | xargs)
    local verdict="${row%%|*}"
    local risk="${row##*|}"
    if [ -z "$verdict" ]; then
        record_stage "6 verdict" "fail" "verdict is NULL — pipeline did not persist output"
        return 1
    fi
    if [ "$verdict" = "benign" ] && [ "$risk" = "5" ]; then
        record_stage "6 verdict" "pass" "verdict=benign risk_score=5"
        return 0
    fi
    record_stage "6 verdict" "degraded" "unexpected verdict=$verdict risk_score=$risk"
    return 0
}

# =============================================================================
# Stage 7 — cleanup tag
# =============================================================================
stage7_cleanup() {
    if [ "$SKIP_CLEANUP" = true ]; then
        record_stage "7 cleanup" "skip" "row left untouched (--skip-cleanup)"
        return 0
    fi
    local out
    out=$(_pg_exec "UPDATE agent_tasks SET input = jsonb_set(COALESCE(input, '{}'::jsonb), '{_probe_cleanup}', 'true'::jsonb) WHERE id = '$TASK_ID'" 2>&1 || echo "FAIL")
    if echo "$out" | grep -qi "error\|fail"; then
        record_stage "7 cleanup" "degraded" "UPDATE returned: $(echo "$out" | head -c 80)"
        return 0
    fi
    record_stage "7 cleanup" "pass" "_probe_cleanup flag set"
    return 0
}

# =============================================================================
# Output
# =============================================================================
compute_overall() {
    local have_fail=false have_degraded=false s
    for s in "${STAGES_STATUS[@]}"; do
        case "$s" in
            fail) have_fail=true ;;
            degraded) have_degraded=true ;;
        esac
    done
    if $have_fail; then
        echo "fail"
    elif $have_degraded; then
        echo "degraded"
    else
        echo "pass"
    fi
}

render_table() {
    local overall="$1"
    local total_ms
    total_ms=$(( $(_now_ms) - PROBE_START_MS ))

    printf '\n%sE2E Pipeline Probe — probe_id: %s%s\n' "$C_BOLD" "${PROBE_ID:0:8}…" "$C_RESET"
    printf '%s\n' "--------------------------------------------------------------------"
    printf '  %-22s %-8s %-6s %-8s %-9s %s\n' "STAGE" "AT" "+ms" "HOP" "STATUS" "DETAIL"
    printf '%s\n' "--------------------------------------------------------------------"
    local i name status at_ms hop_ms detail plus_ms at_hms
    for i in "${!STAGES_NAMES[@]}"; do
        name="${STAGES_NAMES[$i]}"
        status="${STAGES_STATUS[$i]}"
        at_ms="${STAGES_AT_MS[$i]}"
        hop_ms="${STAGES_HOP_MS[$i]}"
        detail="${STAGES_DETAIL[$i]}"
        plus_ms=$(( at_ms - PROBE_START_MS ))
        # Convert at_ms to HH:MM:SS.mmm (best-effort; BSD date differs)
        local at_s=$(( at_ms / 1000 ))
        local at_mms=$(( at_ms % 1000 ))
        at_hms=$(date -u -d "@$at_s" +%H:%M:%S 2>/dev/null || date -u -r "$at_s" +%H:%M:%S 2>/dev/null || echo "--:--:--")
        if [ ${#detail} -gt 40 ]; then
            detail="${detail:0:37}..."
        fi
        local color
        color=$(_status_color "$status")
        printf '  %-22s %s.%03d %5d %6sms %s%-9s%s %s\n' \
            "$name" "$at_hms" "$at_mms" "$plus_ms" "$hop_ms" \
            "$color" "$status" "$C_RESET" "$detail"
    done
    printf '%s\n' "--------------------------------------------------------------------"
    if [ -n "$STALL_STAGE" ]; then
        printf '  %sSTALL:%s %s\n' "$C_RED" "$C_RESET" "$STALL_STAGE"
    fi
    local overall_color
    overall_color=$(_status_color "$overall")
    printf '  %sOVERALL: %s%s%s  total: %dms\n\n' "$C_BOLD" "$overall_color" "$overall" "$C_RESET" "$total_ms"
}

render_json() {
    local overall="$1"
    local total_ms=$(( $(_now_ms) - PROBE_START_MS ))
    local stall_json="null"
    if [ -n "$STALL_STAGE" ]; then
        stall_json=$(jq -n --arg s "$STALL_STAGE" '$s')
    fi
    local stages_json="[]"
    if [ ${#STAGES_NAMES[@]} -gt 0 ]; then
        local i entries=()
        for i in "${!STAGES_NAMES[@]}"; do
            entries+=("$(jq -cn \
                --arg name "${STAGES_NAMES[$i]}" \
                --arg status "${STAGES_STATUS[$i]}" \
                --argjson at_ms "${STAGES_AT_MS[$i]}" \
                --argjson hop_ms "${STAGES_HOP_MS[$i]}" \
                --arg detail "${STAGES_DETAIL[$i]}" \
                '{name:$name,status:$status,at_ms:$at_ms,hop_ms:$hop_ms,detail:$detail}')")
        done
        stages_json=$(printf '%s\n' "${entries[@]}" | jq -s '.')
    fi
    jq -n \
        --arg probe_id "$PROBE_ID" \
        --arg task_id "$TASK_ID" \
        --arg overall "$overall" \
        --argjson total_ms "$total_ms" \
        --argjson stall_stage "$stall_json" \
        --argjson stages "$stages_json" \
        '{schema_version:1, probe_id:$probe_id, task_id:$task_id, overall:$overall, total_ms:$total_ms, stall_stage:$stall_stage, stages:$stages}'
}

# =============================================================================
# Main
# =============================================================================
main() {
    PROBE_START_MS=$(_now_ms)
    LAST_STAGE_MS=0

    # Stage 0 — setup is mandatory. On failure, everything else is skipped.
    if ! stage0_setup; then
        local overall
        overall=$(compute_overall)
        if $OUTPUT_JSON; then render_json "$overall"; else render_table "$overall"; fi
        exit 1
    fi

    # database-seed-system: --check-fixture short-circuits after Stage 0 so
    # operators can gate on "is the seed applied?" without running the
    # full pipeline probe.
    if [ "$CHECK_FIXTURE_ONLY" = true ]; then
        if $OUTPUT_JSON; then
            jq -n \
                --arg probe_id "$PROBE_ID" \
                --arg tenant "$TENANT_ID" \
                '{schema_version:1, overall:"pass", check:"fixture", probe_id:$probe_id, tenant_id:$tenant, message:"token obtained for admin@test.local"}'
        else
            printf '\n%sfixture OK%s: token obtained for %s (tenant=%s)\n\n' \
                "$C_GREEN" "$C_RESET" "$ZOVARK_PROBE_EMAIL" "${TENANT_ID:0:8}…"
        fi
        exit 0
    fi

    # From here on, each failure halts the chain but we still render what we have.
    local chain_broken=false
    for fn in stage05_db_write stage06_schema_ledger stage1_ingest stage2_redpanda stage3_pg_investigating stage4_pg_completed stage5_signoz stage6_verdict stage7_cleanup; do
        if $chain_broken; then
            record_stage "${fn#stage*_}" "skip" "prior stage failed"
            continue
        fi
        if _exceeded_total_timeout; then
            record_stage "${fn#stage*_}" "fail" "total timeout (${TOTAL_TIMEOUT}s) exceeded"
            chain_broken=true
            continue
        fi
        if ! "$fn"; then
            chain_broken=true
        fi
    done

    local overall
    overall=$(compute_overall)
    if $OUTPUT_JSON; then
        render_json "$overall"
    else
        render_table "$overall"
    fi
    case "$overall" in
        pass) exit 0 ;;
        degraded) exit 2 ;;
        *) exit 1 ;;
    esac
}

main "$@"
