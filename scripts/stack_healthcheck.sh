#!/usr/bin/env bash
# ============================================================================
# Zovark Stack Healthcheck
#
# Point-in-time probe of every runtime component of a running Zovark stack.
# Run it after `docker compose up -d` to confirm the whole stack is live.
#
# Probes:
#   api        — GET $ZOVARK_API_BASE/ready (expects .status == "ready")
#   dashboard  — GET $ZOVARK_DASHBOARD_BASE/health (expects HTTP 200)
#   signoz     — GET $ZOVARK_SIGNOZ_BASE/api/v1/health (expects 200) AND
#                GET $ZOVARK_SIGNOZ_BASE/api/v1/services (10-min lookback) must
#                return a non-empty JSON array. Empty → "degraded", not fail.
#   healer     — GET $ZOVARK_HEALER_BASE/api/health (expects 200)
#   redpanda   — TCP connect 127.0.0.1:19092 via /dev/tcp, wrapped in timeout
#   valkey     — docker compose exec redis valkey-cli -a $REDIS_PASSWORD ping
#   temporal   — docker compose exec temporal tctl cluster health (SERVING)
#   postgres   — docker compose exec postgres pg_isready -U zovark -d zovark
#
# Env var contract:
#   ZOVARK_API_BASE         default http://127.0.0.1:8090
#   ZOVARK_DASHBOARD_BASE   default http://127.0.0.1:3000
#   ZOVARK_SIGNOZ_BASE      default http://127.0.0.1:3301
#   ZOVARK_HEALER_BASE      default http://127.0.0.1:8081
#   REDIS_PASSWORD          required for valkey probe (no default — fail loud)
#   TIMEOUT                 per-probe timeout in seconds, default 5
#
# Flags:
#   --json              emit one JSON object to stdout, implies --no-color
#   --no-color          suppress ANSI escapes even on a TTY
#   --skip a,b,c        skip comma-separated list of probe names
#   --timeout N         override per-probe timeout (seconds)
#   --help              print usage and exit 0
#
# Exit codes:
#   0  every non-skipped probe passed
#   1  at least one probe failed
#   2  no failures but at least one degraded, OR jq is missing
#
# Safety:
#   Read-only. No writes, no restarts, no state mutation. Safe to run
#   repeatedly from any account that has docker CLI access.
#
# Adding a new probe:
#   1. Write check_<name>() that calls one of the _http_check / _tcp_check /
#      _compose_exec_check helpers, or emit() directly.
#   2. Append "<name>" to the CHECKS array below.
# ============================================================================

set -euo pipefail
MSYS_NO_PATHCONV=1  # Git-Bash on Windows: stop path translation breaking URLs.

# ---------------------------------------------------------------------------- jq guard
if ! command -v jq >/dev/null 2>&1; then
    echo "ERROR: jq is required but not installed." >&2
    echo "       Install hint: apt install jq  (Debian/Ubuntu)" >&2
    echo "                     brew install jq (macOS)" >&2
    echo "                     pacman -S jq    (Arch)" >&2
    exit 2
fi

# ---------------------------------------------------------------------------- Defaults
ZOVARK_API_BASE="${ZOVARK_API_BASE:-http://127.0.0.1:8090}"
ZOVARK_DASHBOARD_BASE="${ZOVARK_DASHBOARD_BASE:-http://127.0.0.1:3000}"
ZOVARK_SIGNOZ_BASE="${ZOVARK_SIGNOZ_BASE:-http://127.0.0.1:3301}"
ZOVARK_HEALER_BASE="${ZOVARK_HEALER_BASE:-http://127.0.0.1:8081}"
TIMEOUT="${TIMEOUT:-5}"

OUTPUT_JSON=false
NO_COLOR=false
SKIP_CSV=""
# Audit e2e-pipeline-probe: --e2e flag chains scripts/e2e_probe.sh after all
# other probes have run. Default off because the e2e probe has side effects
# (one synthetic row in agent_tasks).
RUN_E2E=false
# telemetry-audit-fix: --debug, --prime, --no-warmup.
DEBUG=0
RUN_PRIME=false
NO_WARMUP=0

# telemetry-audit-fix env overrides for the signoz warmup flow.
ZOVARK_SIGNOZ_WARMUP_URL="${ZOVARK_SIGNOZ_WARMUP_URL:-${ZOVARK_API_BASE}/ready}"
ZOVARK_SIGNOZ_WARMUP_SLEEP_SEC="${ZOVARK_SIGNOZ_WARMUP_SLEEP_SEC:-3}"
# Required services for the new dedicated telemetry probe.
ZOVARK_TELEMETRY_REQUIRED_SERVICES="${ZOVARK_TELEMETRY_REQUIRED_SERVICES:-zovark-api,zovark-worker}"

# Canonical probe order. Adding a new probe = append here + define check_<name>.
# telemetry-audit-fix: telemetry inserted after signoz.
CHECKS=(api dashboard signoz telemetry healer redpanda valkey temporal postgres)

# ---------------------------------------------------------------------------- Usage / CLI
usage() {
    cat <<'EOF'
Usage: scripts/stack_healthcheck.sh [OPTIONS]

Probe every runtime component of a running Zovark stack and print a
colored pass/fail table (or a JSON object with --json).

OPTIONS:
  --json              Emit a single JSON object to stdout (implies --no-color).
                      Schema: {"schema_version":1,"overall":...,"checks":[...]}
  --no-color          Suppress ANSI color escapes, even on a TTY.
  --skip LIST         Comma-separated list of probes to skip.
                      Valid probe names:
                        api, dashboard, signoz, healer, redpanda,
                        valkey, temporal, postgres
                      Example: --skip signoz,redpanda
  --timeout SECONDS   Per-probe timeout in seconds (default: 5).
  --e2e               After every other probe has run, chain
                      scripts/e2e_probe.sh --json as a final "e2e" probe.
                      Writes one tagged synthetic row to agent_tasks.
  --prime             Before any probes run, invoke scripts/e2e_probe.sh
                      --check-fixture to tickle the API and generate a span
                      from zovark-api. Use this on cold/idle stacks so the
                      signoz + telemetry probes report `pass` on the first
                      run instead of a false `degraded`.
  --no-warmup         Skip the check_signoz warmup request. The probe stays
                      purely observational (may report `degraded` on idle).
  --debug             Print the discovered container map, raw HTTP response
                      bodies on failing probes, and docker exec stderr on
                      failing container probes. Implies --no-color.
  --help              Show this message and exit 0.

VALID PROBE NAMES (for --skip):
  api, dashboard, signoz, telemetry, healer, redpanda, valkey, temporal, postgres

ENV VARS:
  ZOVARK_API_BASE         API base URL         (default: http://127.0.0.1:8090)
  ZOVARK_DASHBOARD_BASE   Dashboard base URL   (default: http://127.0.0.1:3000)
  ZOVARK_SIGNOZ_BASE      Signoz frontend URL  (default: http://127.0.0.1:3301)
  ZOVARK_HEALER_BASE      Healer base URL      (default: http://127.0.0.1:8081)
  REDIS_PASSWORD          Required for the valkey probe (no safe default).
  TIMEOUT                 Override the default per-probe timeout.

EXIT CODES:
  0   every non-skipped probe passed
  1   at least one probe failed
  2   no failures but at least one degraded, or jq missing

EXAMPLES:
  scripts/stack_healthcheck.sh
  scripts/stack_healthcheck.sh --skip signoz,redpanda
  scripts/stack_healthcheck.sh --json --timeout 10
  ZOVARK_API_BASE=http://staging-host:8090 scripts/stack_healthcheck.sh
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --json) OUTPUT_JSON=true; NO_COLOR=true; shift ;;
        --no-color) NO_COLOR=true; shift ;;
        --skip)
            if [ $# -lt 2 ]; then echo "ERROR: --skip requires an argument" >&2; exit 2; fi
            SKIP_CSV="$2"; shift 2 ;;
        --skip=*) SKIP_CSV="${1#--skip=}"; shift ;;
        --timeout)
            if [ $# -lt 2 ]; then echo "ERROR: --timeout requires an argument" >&2; exit 2; fi
            TIMEOUT="$2"; shift 2 ;;
        --timeout=*) TIMEOUT="${1#--timeout=}"; shift ;;
        --e2e) RUN_E2E=true; shift ;;
        --prime) RUN_PRIME=true; shift ;;
        --no-warmup) NO_WARMUP=1; shift ;;
        --debug) DEBUG=1; NO_COLOR=true; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "ERROR: unknown flag: $1" >&2; usage >&2; exit 2 ;;
    esac
done

# Normalize skip list for quick lookup (space-separated).
SKIP_LIST=" $(echo "$SKIP_CSV" | tr ',' ' ') "

_is_skipped() {
    case "$SKIP_LIST" in
        *" $1 "*) return 0 ;;
        *) return 1 ;;
    esac
}

# ---------------------------------------------------------------------------- Color handling
if [ "$NO_COLOR" = false ] && [ -t 1 ]; then
    C_RESET=$'\033[0m'
    C_BOLD=$'\033[1m'
    C_GREEN=$'\033[32m'
    C_RED=$'\033[31m'
    C_YELLOW=$'\033[33m'
    C_GRAY=$'\033[90m'
    C_CYAN=$'\033[36m'
else
    C_RESET=""
    C_BOLD=""
    C_GREEN=""
    C_RED=""
    C_YELLOW=""
    C_GRAY=""
    C_CYAN=""
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

_status_glyph() {
    case "$1" in
        pass) printf '✓' ;;
        fail) printf '✗' ;;
        degraded) printf '~' ;;
        skip) printf '-' ;;
        *) printf '?' ;;
    esac
}

# ---------------------------------------------------------------------------- Probe primitives
CHECK_STATUS=""
CHECK_LATENCY_MS=0
CHECK_DETAIL=""

emit() {
    # emit <status> <latency_ms> <detail>
    CHECK_STATUS="$1"
    CHECK_LATENCY_MS="$2"
    CHECK_DETAIL="$3"
}

_now_ms() {
    # Prefer GNU date %N; fall back to Python if nanos unsupported (macOS BSD date).
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
    # Last-resort: seconds × 1000.
    printf '%d' "$(( $(date +%s) * 1000 ))"
}

_http_check() {
    # _http_check <name> <url> <jq_success_filter>
    # Runs curl -fsS, measures latency, asserts the response body satisfies the jq filter.
    local name="$1" url="$2" filter="$3"
    local t0 t1 latency body rc
    LAST_DEBUG_BODY=""
    t0=$(_now_ms)
    if body=$(curl -fsS --max-time "$TIMEOUT" "$url" 2>&1); then
        rc=0
    else
        rc=$?
    fi
    t1=$(_now_ms)
    latency=$(( t1 - t0 ))
    if [ $rc -ne 0 ]; then
        LAST_DEBUG_BODY="$body"
        # telemetry-audit-fix: include the curl exit code in the detail so
        # operators can tell "network unreachable" (7) from "timeout" (28).
        emit fail "$latency" "curl exit $rc: $(echo "$body" | head -c 100)"
        return
    fi
    # Try to assert via jq; if body isn't JSON, empty filter '.' still matches anything non-empty.
    if [ -z "$filter" ] || [ "$filter" = "." ]; then
        if [ -z "$body" ]; then
            emit fail "$latency" "empty response body"
        else
            emit pass "$latency" "HTTP 200 (${#body}B)"
        fi
        return
    fi
    if echo "$body" | jq -e "$filter" >/dev/null 2>&1; then
        emit pass "$latency" "matched: $filter"
    else
        emit fail "$latency" "body did not match: $filter"
    fi
}

_tcp_check() {
    # _tcp_check <name> <host> <port>
    # Uses bash /dev/tcp built-in wrapped in `timeout` so a hung listener can't block.
    local name="$1" host="$2" port="$3"
    local t0 t1 latency
    t0=$(_now_ms)
    if timeout "$TIMEOUT" bash -c "</dev/tcp/$host/$port" >/dev/null 2>&1; then
        t1=$(_now_ms)
        latency=$(( t1 - t0 ))
        emit pass "$latency" "tcp ${host}:${port} open"
    else
        t1=$(_now_ms)
        latency=$(( t1 - t0 ))
        emit fail "$latency" "tcp ${host}:${port} unreachable"
    fi
}

# ---------------------------------------------------------------------------- Container discovery
# telemetry-audit-fix D12: cache a `service → container_name` map via a single
# `docker compose ps --format json` call at script start. Falls back to
# `docker ps --filter name=zovark-<alias>` when the compose JSON is empty or
# missing a service. Probes then call `docker exec <container-name>` directly,
# removing the dependency on `docker compose exec <service>` (which requires
# the compose CLI's working-directory + project-name to match).
declare -A COMPOSE_MAP=()
COMPOSE_MAP_LOADED=false

_discover_compose_containers() {
    [ "$COMPOSE_MAP_LOADED" = true ] && return 0
    COMPOSE_MAP_LOADED=true
    if ! command -v docker >/dev/null 2>&1; then
        return 0
    fi
    local raw
    raw=$(docker compose ps --format json 2>/dev/null || true)
    [ -z "$raw" ] && return 0

    # Handle both JSON-array and newline-delimited-JSON compose output formats.
    # jq --slurp reads the whole input as an array; the `if type == "array"`
    # flattens nested arrays that older compose emits.
    while IFS=$'\t' read -r svc name; do
        [ -z "$svc" ] && continue
        [ -z "$name" ] && continue
        COMPOSE_MAP[$svc]="$name"
    done < <(printf '%s' "$raw" | jq -rs '
        map(if type == "array" then .[] else . end)
        | .[]
        | [(.Service // .service // ""), (.Name // .name // "")]
        | @tsv
    ' 2>/dev/null || true)
}

_fallback_container_alias() {
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

_compose_container_name() {
    local service="$1"
    _discover_compose_containers
    local name="${COMPOSE_MAP[$service]:-}"
    if [ -n "$name" ]; then
        printf '%s' "$name"
        return 0
    fi
    local alias
    alias=$(_fallback_container_alias "$service")
    if [ -z "$alias" ]; then
        return 1
    fi
    # Exact match first, then prefix (covers scaled containers like zovark-worker-1).
    name=$(docker ps --filter "name=^${alias}$" --format '{{.Names}}' 2>/dev/null | head -1)
    if [ -z "$name" ]; then
        name=$(docker ps --filter "name=^${alias}" --format '{{.Names}}' 2>/dev/null | head -1)
    fi
    if [ -n "$name" ]; then
        printf '%s' "$name"
        return 0
    fi
    return 1
}

# Last-probe-failure debug payload, populated by _compose_exec_check and
# _http_check when DEBUG=1 and the probe emits fail or degraded. Printed
# indented under the probe's table row at render time.
LAST_DEBUG_BODY=""

_compose_exec_check() {
    # _compose_exec_check <name> <service> <command...>
    # Resolves the container name via _compose_container_name and runs the
    # command via `docker exec`. If the container can't be resolved at all,
    # the probe emits skip.
    local name="$1" service="$2"
    shift 2
    local t0 t1 latency out rc container
    LAST_DEBUG_BODY=""
    if ! container=$(_compose_container_name "$service"); then
        emit skip 0 "container '$service' not running"
        return
    fi
    t0=$(_now_ms)
    if out=$(docker exec -i "$container" "$@" 2>&1); then
        rc=0
    else
        rc=$?
    fi
    t1=$(_now_ms)
    latency=$(( t1 - t0 ))
    if [ $rc -ne 0 ]; then
        LAST_DEBUG_BODY="$out"
        emit fail "$latency" "exit $rc: $(echo "$out" | head -c 160)"
        return
    fi
    emit pass "$latency" "$(echo "$out" | head -c 120)"
}

# ---------------------------------------------------------------------------- Individual probes
check_api() {
    _http_check api "${ZOVARK_API_BASE}/ready" '.status == "ready"'
}

check_dashboard() {
    # Dashboard /health returns plain "ok", not JSON. Use the raw-body variant.
    _http_check dashboard "${ZOVARK_DASHBOARD_BASE}/health" '.'
}

check_signoz() {
    # telemetry-audit-fix D6: warmup flow.
    # 1. GET /api/v1/health — fail if Signoz itself is down
    # 2. Unless NO_WARMUP=1, curl $ZOVARK_SIGNOZ_WARMUP_URL once to emit a span
    #    from the API via otelgin.Middleware, then sleep $ZOVARK_SIGNOZ_WARMUP_SLEEP_SEC
    #    for the BatchSpanProcessor to flush
    # 3. Query /api/v1/services with a 2-minute window
    # 4. pass on non-empty, degraded on empty (after successful warmup),
    #    fail with distinct "warmup failure" detail on non-2xx warmup
    local t0 t1 latency body_health body_services services_count
    LAST_DEBUG_BODY=""
    t0=$(_now_ms)
    if ! body_health=$(curl -fsS --max-time "$TIMEOUT" "${ZOVARK_SIGNOZ_BASE}/api/v1/health" 2>&1); then
        t1=$(_now_ms)
        LAST_DEBUG_BODY="$body_health"
        emit fail "$(( t1 - t0 ))" "signoz /api/v1/health unreachable: $(echo "$body_health" | head -c 120)"
        return
    fi

    if [ "$NO_WARMUP" != "1" ]; then
        # Capture stderr + http_code separately. curl writes the HTTP status
        # to stdout via -w and connection errors to stderr; we want the stderr
        # as a debug body, the code as a plain 3-digit string (000 on connect
        # failure, so the 2xx prefix test is the right gate).
        local warmup_code warmup_err
        warmup_err=$(mktemp)
        warmup_code=$(curl -sS --max-time "$TIMEOUT" -o /dev/null \
            -w '%{http_code}' "$ZOVARK_SIGNOZ_WARMUP_URL" 2>"$warmup_err" || true)
        warmup_code="${warmup_code:-000}"
        if [[ ! "$warmup_code" =~ ^2 ]]; then
            t1=$(_now_ms)
            LAST_DEBUG_BODY="warmup GET $ZOVARK_SIGNOZ_WARMUP_URL returned HTTP=$warmup_code; stderr=$(head -c 200 "$warmup_err" 2>/dev/null || true)"
            rm -f "$warmup_err"
            emit fail "$(( t1 - t0 ))" "warmup failure: HTTP $warmup_code ($ZOVARK_SIGNOZ_WARMUP_URL)"
            return
        fi
        rm -f "$warmup_err"
        sleep "$ZOVARK_SIGNOZ_WARMUP_SLEEP_SEC"
    fi

    # Two-minute lookback — tighter than the old 10-minute window, because with
    # the warmup above the span should have landed within a few seconds.
    local end_ms start_ms
    end_ms=$(_now_ms)
    start_ms=$(( end_ms - 2 * 60 * 1000 ))
    if ! body_services=$(curl -fsS --max-time "$TIMEOUT" \
        "${ZOVARK_SIGNOZ_BASE}/api/v1/services?start=${start_ms}&end=${end_ms}" 2>&1); then
        t1=$(_now_ms)
        latency=$(( t1 - t0 ))
        LAST_DEBUG_BODY="$body_services"
        emit degraded "$latency" "/api/v1/health OK but /api/v1/services unreachable"
        return
    fi
    services_count=$(echo "$body_services" | jq -r 'if type == "array" then length
                                                   elif (.data | type) == "array" then (.data | length)
                                                   elif (.services | type) == "array" then (.services | length)
                                                   else 0 end' 2>/dev/null || echo "0")
    t1=$(_now_ms)
    latency=$(( t1 - t0 ))
    if [ "$services_count" -ge 1 ] 2>/dev/null; then
        local warmup_tag=""
        [ "$NO_WARMUP" = "1" ] && warmup_tag=" (no warmup)"
        emit pass "$latency" "${services_count} service(s) in 2-min window${warmup_tag}"
    else
        LAST_DEBUG_BODY="$body_services"
        if [ "$NO_WARMUP" = "1" ]; then
            emit degraded "$latency" "ingest empty (no services in 2-min window; warmup skipped)"
        else
            emit degraded "$latency" "ingest empty (warmup landed but no services yet — collector→clickhouse?)"
        fi
    fi
}

check_telemetry() {
    # telemetry-audit-fix D9: verify specific required services have reported
    # spans, distinct from check_signoz's general "Signoz is alive" check.
    local t0 t1 latency body services_csv required_csv
    LAST_DEBUG_BODY=""
    t0=$(_now_ms)
    local end_ms start_ms
    end_ms=$(_now_ms)
    start_ms=$(( end_ms - 10 * 60 * 1000 ))
    if ! body=$(curl -fsS --max-time "$TIMEOUT" \
        "${ZOVARK_SIGNOZ_BASE}/api/v1/services?start=${start_ms}&end=${end_ms}" 2>&1); then
        t1=$(_now_ms)
        LAST_DEBUG_BODY="$body"
        emit fail "$(( t1 - t0 ))" "signoz /api/v1/services unreachable"
        return
    fi
    # Extract every service name across response-shape variants.
    services_csv=$(echo "$body" | jq -r '
        [ (.data // [])[]?.serviceName,
          (.services // [])[]?.serviceName,
          (if type == "array" then .[]?.serviceName else empty end) ]
        | map(select(. != null)) | unique | join(",")
    ' 2>/dev/null || echo "")
    t1=$(_now_ms)
    latency=$(( t1 - t0 ))

    required_csv="$ZOVARK_TELEMETRY_REQUIRED_SERVICES"
    local present=0 missing=()
    local req
    for req in ${required_csv//,/ }; do
        if [[ ",$services_csv," == *",$req,"* ]]; then
            present=$(( present + 1 ))
        else
            missing+=("$req")
        fi
    done
    local total=0
    for req in ${required_csv//,/ }; do total=$(( total + 1 )); done

    if [ "$present" -eq "$total" ]; then
        emit pass "$latency" "all required services reporting: $required_csv"
    elif [ "$present" -gt 0 ]; then
        local missing_csv
        missing_csv=$(IFS=,; echo "${missing[*]}")
        LAST_DEBUG_BODY="services seen: $services_csv"
        emit degraded "$latency" "missing: $missing_csv"
    else
        LAST_DEBUG_BODY="services seen: $services_csv"
        emit fail "$latency" "no required services reporting: $required_csv"
    fi
}

check_healer() {
    _http_check healer "${ZOVARK_HEALER_BASE}/api/health" '.'
}

check_redpanda() {
    _tcp_check redpanda 127.0.0.1 19092
}

check_valkey() {
    if [ -z "${REDIS_PASSWORD:-}" ]; then
        emit fail 0 "REDIS_PASSWORD env var is required for the valkey probe"
        return
    fi
    _compose_exec_check valkey redis valkey-cli -a "$REDIS_PASSWORD" ping
    # Upgrade: valkey-cli prints a warning about -a on stderr but returns PONG on stdout.
    # Our _compose_exec_check already captures combined output; check for PONG explicitly
    # so a warning doesn't dilute the detail message.
    if [ "$CHECK_STATUS" = "pass" ] && [[ "$CHECK_DETAIL" != *PONG* ]]; then
        emit fail "$CHECK_LATENCY_MS" "valkey responded without PONG: $CHECK_DETAIL"
    fi
}

check_temporal() {
    _compose_exec_check temporal temporal tctl --address temporal:7233 cluster health
    # tctl prints to stderr on some versions; require SERVING in the captured output.
    if [ "$CHECK_STATUS" = "pass" ] && [[ "$CHECK_DETAIL" != *SERVING* ]]; then
        emit degraded "$CHECK_LATENCY_MS" "tctl exited 0 but SERVING not in output: $CHECK_DETAIL"
    fi
}

check_postgres() {
    _compose_exec_check postgres postgres pg_isready -U zovark -d zovark
}

# ---------------------------------------------------------------------------- Runner
RESULTS_NAMES=()
RESULTS_STATUS=()
RESULTS_LATENCY=()
RESULTS_DETAIL=()
RESULTS_DEBUG=()

# telemetry-audit-fix: run the compose discovery sweep once before any probes,
# so --debug can print the map and so the first probe isn't delayed by it.
_discover_compose_containers
if [ "$DEBUG" = "1" ]; then
    printf '%s[debug] container map:%s\n' "$C_CYAN" "$C_RESET"
    if [ "${#COMPOSE_MAP[@]}" -eq 0 ]; then
        printf '  %s(empty — compose ps returned nothing; fallbacks will be used)%s\n' \
            "$C_GRAY" "$C_RESET"
    else
        local_svc=""
        for local_svc in "${!COMPOSE_MAP[@]}"; do
            printf '  %s=%s\n' "$local_svc" "${COMPOSE_MAP[$local_svc]}"
        done
    fi
    printf '\n'
fi

# telemetry-audit-fix D7: --prime runs scripts/e2e_probe.sh --check-fixture
# BEFORE any probes so the API emits at least one span. Makes signoz + telemetry
# probes report `pass` on first run against a cold stack.
if [ "$RUN_PRIME" = true ]; then
    _prime_script="$(dirname -- "$0")/e2e_probe.sh"
    if [ ! -x "$_prime_script" ]; then
        printf '%s[fail] --prime: e2e_probe.sh not executable at %s%s\n' \
            "$C_RED" "$_prime_script" "$C_RESET" >&2
        exit 1
    fi
    set +e
    _prime_out=$("$_prime_script" --check-fixture --json 2>&1)
    _prime_rc=$?
    set -e
    if [ $_prime_rc -ne 0 ]; then
        printf '%s[fail] --prime: check-fixture exited %d%s\n' "$C_RED" "$_prime_rc" "$C_RESET" >&2
        printf '%s\n' "$_prime_out" | head -c 400 >&2
        printf '\n' >&2
        exit 1
    fi
    if [ "$DEBUG" = "1" ]; then
        printf '%s[debug] --prime ok:%s %s\n\n' "$C_CYAN" "$C_RESET" \
            "$(printf '%s' "$_prime_out" | jq -c '{probe_id, tenant_id}' 2>/dev/null || echo 'ok')"
    fi
fi

for name in "${CHECKS[@]}"; do
    LAST_DEBUG_BODY=""
    if _is_skipped "$name"; then
        CHECK_STATUS="skip"
        CHECK_LATENCY_MS=0
        CHECK_DETAIL="skipped via --skip"
    else
        # Call the check function. Disable `errexit` for the duration so a
        # failing sub-command inside the check can't abort the whole runner.
        set +e
        "check_$name"
        set -e
    fi
    RESULTS_NAMES+=("$name")
    RESULTS_STATUS+=("$CHECK_STATUS")
    RESULTS_LATENCY+=("$CHECK_LATENCY_MS")
    RESULTS_DETAIL+=("$CHECK_DETAIL")
    RESULTS_DEBUG+=("$LAST_DEBUG_BODY")
done

# ---------------------------------------------------------------------------- Optional e2e chain
# Audit e2e-pipeline-probe task 13: when --e2e is set, run scripts/e2e_probe.sh
# in --json mode after all other probes and fold the result into RESULTS as a
# single "e2e" row. The probe's own exit code maps to our status taxonomy:
# exit 0 → pass, exit 2 → degraded, anything else → fail.
if [ "$RUN_E2E" = true ]; then
    _e2e_t0=$(date +%s%3N 2>/dev/null || echo "0")
    _e2e_script_path="$(dirname -- "$0")/e2e_probe.sh"
    if [ ! -x "$_e2e_script_path" ]; then
        RESULTS_NAMES+=("e2e")
        RESULTS_STATUS+=("fail")
        RESULTS_LATENCY+=(0)
        RESULTS_DETAIL+=("e2e_probe.sh not executable at $_e2e_script_path")
        RESULTS_DEBUG+=("")
    else
        # Run with --json so we can parse a single line of output.
        set +e
        _e2e_out=$("$_e2e_script_path" --json 2>&1)
        _e2e_rc=$?
        set -e
        _e2e_t1=$(date +%s%3N 2>/dev/null || echo "$_e2e_t0")
        _e2e_latency=$(( _e2e_t1 - _e2e_t0 ))
        _e2e_overall="unknown"
        _e2e_stall=""
        if echo "$_e2e_out" | jq -e . >/dev/null 2>&1; then
            _e2e_overall=$(echo "$_e2e_out" | jq -r '.overall // "unknown"')
            _e2e_stall=$(echo "$_e2e_out" | jq -r '.stall_stage // ""')
        fi
        # Map probe exit to stack_healthcheck status taxonomy.
        case "$_e2e_rc" in
            0) _e2e_status="pass" ;;
            2) _e2e_status="degraded" ;;
            *) _e2e_status="fail" ;;
        esac
        _e2e_detail="overall=$_e2e_overall"
        if [ -n "$_e2e_stall" ] && [ "$_e2e_stall" != "null" ]; then
            _e2e_detail="$_e2e_detail stall=$_e2e_stall"
        fi
        RESULTS_NAMES+=("e2e")
        RESULTS_STATUS+=("$_e2e_status")
        RESULTS_LATENCY+=("$_e2e_latency")
        RESULTS_DETAIL+=("$_e2e_detail")
        RESULTS_DEBUG+=("$_e2e_out")
    fi
fi

# ---------------------------------------------------------------------------- Compute overall
OVERALL="pass"
have_fail=false
have_degraded=false
for s in "${RESULTS_STATUS[@]}"; do
    case "$s" in
        fail) have_fail=true ;;
        degraded) have_degraded=true ;;
    esac
done
if $have_fail; then
    OVERALL="fail"
elif $have_degraded; then
    OVERALL="degraded"
fi

# ---------------------------------------------------------------------------- Output
if $OUTPUT_JSON; then
    # Build JSON via jq -n to avoid any quoting pitfalls.
    jq -n \
        --arg overall "$OVERALL" \
        --argjson checks "$(
            for i in "${!RESULTS_NAMES[@]}"; do
                jq -n \
                    --arg name "${RESULTS_NAMES[$i]}" \
                    --arg status "${RESULTS_STATUS[$i]}" \
                    --argjson latency_ms "${RESULTS_LATENCY[$i]}" \
                    --arg detail "${RESULTS_DETAIL[$i]}" \
                    '{name:$name, status:$status, latency_ms:$latency_ms, detail:$detail}'
            done | jq -s '.'
        )" \
        '{schema_version: 1, overall: $overall, checks: $checks}'
else
    printf '\n%sZovark Stack Healthcheck%s\n' "$C_BOLD" "$C_RESET"
    printf '%s\n' "------------------------------------------------------------------------"
    printf '  %-12s %-4s %-10s %-8s  %s\n' "PROBE" "" "STATUS" "LATENCY" "DETAIL"
    printf '%s\n' "------------------------------------------------------------------------"
    for i in "${!RESULTS_NAMES[@]}"; do
        local_name="${RESULTS_NAMES[$i]}"
        local_status="${RESULTS_STATUS[$i]}"
        local_latency="${RESULTS_LATENCY[$i]}"
        local_detail="${RESULTS_DETAIL[$i]}"
        color=$(_status_color "$local_status")
        glyph=$(_status_glyph "$local_status")
        # Truncate overly long detail strings so the table stays aligned.
        if [ ${#local_detail} -gt 44 ]; then
            local_detail="${local_detail:0:41}..."
        fi
        printf '  %-12s %s%s%s    %s%-10s%s %6sms  %s\n' \
            "$local_name" "$color" "$glyph" "$C_RESET" \
            "$color" "$local_status" "$C_RESET" \
            "$local_latency" "$local_detail"
        # telemetry-audit-fix: --debug prints the captured debug body under
        # the probe row when the probe failed or degraded.
        if [ "$DEBUG" = "1" ] \
            && { [ "$local_status" = "fail" ] || [ "$local_status" = "degraded" ]; } \
            && [ -n "${RESULTS_DEBUG[$i]:-}" ]; then
            local_debug="${RESULTS_DEBUG[$i]}"
            printf '  %s%s%s\n' "$C_GRAY" "--- debug ---" "$C_RESET"
            printf '%s' "$local_debug" | head -c 800 | sed 's/^/    /'
            printf '\n  %s%s%s\n' "$C_GRAY" "---" "$C_RESET"
        fi
    done
    printf '%s\n' "------------------------------------------------------------------------"
    overall_color=$(_status_color "$OVERALL")
    printf '  %sOVERALL: %s%s%s\n\n' "$C_BOLD" "$overall_color" "$OVERALL" "$C_RESET"
fi

# ---------------------------------------------------------------------------- Exit code
case "$OVERALL" in
    pass) exit 0 ;;
    degraded) exit 2 ;;
    fail) exit 1 ;;
    *) exit 1 ;;
esac
