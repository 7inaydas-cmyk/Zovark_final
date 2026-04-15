#!/usr/bin/env bash
# =============================================================================
# scripts/seed_dev.sh — manual re-seed helper for Zovark dev fixtures.
#
# Default (no args): applies migrations/seed_dev_data.sql to postgres AND
# pre-creates the dev tenant Redpanda topic (tasks.new.<dev-tenant-uuid>) so
# the worker's pattern-subscribed consumer doesn't have to wait for a
# metadata-refresh window to discover it on first publish.
#
# Safe to run repeatedly; every INSERT in the seed file is ON CONFLICT DO
# NOTHING and `rpk topic create` is treated as idempotent (TOPIC_ALREADY_EXISTS
# is a soft success).
#
# Flags:
#   --check              Query the DB for the three fixture rows AND verify
#                        the dev tenant Redpanda topic exists. Exit 0 if all
#                        present, 2 if partial / topic missing, 1 if the
#                        tenant is missing (= seed never ran).
#   --regenerate-hash    Compute a fresh bcrypt cost-12 hash of the literal
#                        TestPass2026 via Python bcrypt and emit a ready-to-paste
#                        UPDATE snippet. Does NOT modify the running DB or the
#                        seed file.
#   --no-color           Suppress ANSI color escapes.
#   --quiet, -q          Suppress info lines (errors still print).
#   --help, -h           Show this help and exit 0.
#
# Env vars:
#   ZOVARK_PG_CONTAINER         postgres container name override
#                               (default: zovark-postgres, discovered via compose)
#   ZOVARK_REDPANDA_CONTAINER   redpanda container name override
#                               (default: zovark-redpanda, discovered via compose)
#   ZOVARK_PG_USER              default zovark
#   ZOVARK_PG_DB                default zovark
#
# Exit codes:
#   0  seed applied (or --check all-present, or --regenerate-hash printed)
#   1  tenant missing on --check, or psql failure, or regenerate-hash dep missing
#   2  partial fixture state on --check, missing dev tenant topic, or missing
#      Python bcrypt dependency
# =============================================================================

set -euo pipefail
MSYS_NO_PATHCONV=1

_SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
SEED_FILE="$_SCRIPT_DIR/../migrations/seed_dev_data.sql"

ZOVARK_PG_CONTAINER="${ZOVARK_PG_CONTAINER:-}"
ZOVARK_PG_USER="${ZOVARK_PG_USER:-zovark}"
ZOVARK_PG_DB="${ZOVARK_PG_DB:-zovark}"

MODE="seed"
NO_COLOR=false
QUIET=0

# ---------------------------------------------------------------------------- Flags
usage() {
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --check) MODE="check"; shift ;;
        --regenerate-hash) MODE="regenerate"; shift ;;
        --no-color) NO_COLOR=true; shift ;;
        --quiet|-q) QUIET=1; shift ;;
        --help|-h) usage ;;
        *) echo "ERROR: unknown flag: $1" >&2; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------- Colors
if [ "$NO_COLOR" = false ] && [ -t 1 ]; then
    C_RESET=$'\033[0m'
    C_BOLD=$'\033[1m'
    C_GREEN=$'\033[32m'
    C_RED=$'\033[31m'
    C_YELLOW=$'\033[33m'
    C_GRAY=$'\033[90m'
else
    C_RESET=""; C_BOLD=""; C_GREEN=""; C_RED=""; C_YELLOW=""; C_GRAY=""
fi

_info() {
    [ "$QUIET" = "1" ] && return 0
    printf '%s[seed_dev]%s %s\n' "$C_GRAY" "$C_RESET" "$*"
}
_ok()   { printf '%s[seed_dev]%s %s✓%s %s\n' "$C_GRAY" "$C_RESET" "$C_GREEN" "$C_RESET" "$*"; }
_warn() { printf '%s[seed_dev]%s %s~%s %s\n' "$C_GRAY" "$C_RESET" "$C_YELLOW" "$C_RESET" "$*" >&2; }
_fail() { printf '%s[seed_dev]%s %s✗%s %s\n' "$C_GRAY" "$C_RESET" "$C_RED" "$C_RESET" "$*" >&2; }

# ---------------------------------------------------------------------------- Container discovery
_resolve_pg_container() {
    if [ -n "$ZOVARK_PG_CONTAINER" ]; then
        printf '%s' "$ZOVARK_PG_CONTAINER"
        return 0
    fi
    local cid
    # Try docker compose ps -q first.
    cid=$(docker compose ps -q postgres 2>/dev/null | head -1 || true)
    if [ -n "$cid" ]; then
        docker inspect -f '{{.Name}}' "$cid" 2>/dev/null | sed 's|^/||'
        return 0
    fi
    # Fallback: docker ps --filter by known name.
    local name
    name=$(docker ps --filter 'name=^zovark-postgres$' --format '{{.Names}}' 2>/dev/null | head -1 || true)
    if [ -n "$name" ]; then
        printf '%s' "$name"
        return 0
    fi
    return 1
}

_psql() {
    local container="$1"; shift
    docker exec -i "$container" psql -U "$ZOVARK_PG_USER" -d "$ZOVARK_PG_DB" \
        -v ON_ERROR_STOP=1 -tA "$@"
}

# fix-e2e-ingest-stall §2: dev tenant Redpanda topic pre-creation.
# Reserved dev tenant UUID from migrations/seed_dev_data.sql.
DEV_TENANT_UUID="00000000-0000-0000-0000-000000000010"
DEV_TENANT_TOPIC="tasks.new.${DEV_TENANT_UUID}"

_resolve_redpanda_container() {
    if [ -n "${ZOVARK_REDPANDA_CONTAINER:-}" ]; then
        printf '%s' "$ZOVARK_REDPANDA_CONTAINER"
        return 0
    fi
    local cid
    cid=$(docker compose ps -q redpanda 2>/dev/null | head -1 || true)
    if [ -n "$cid" ]; then
        docker inspect -f '{{.Name}}' "$cid" 2>/dev/null | sed 's|^/||'
        return 0
    fi
    local name
    name=$(docker ps --filter 'name=^zovark-redpanda$' --format '{{.Names}}' 2>/dev/null | head -1 || true)
    if [ -n "$name" ]; then
        printf '%s' "$name"
        return 0
    fi
    return 1
}

# Idempotent: rpk topic create returns non-zero with TOPIC_ALREADY_EXISTS in
# stderr if the topic exists. We grep for that and treat it as success.
_create_dev_tenant_topic() {
    local container
    if ! container=$(_resolve_redpanda_container); then
        _warn "redpanda container not running — skipping dev tenant topic pre-create"
        _info "the worker's pattern subscription will discover the topic on first publish"
        return 0
    fi
    _info "redpanda container: $container"
    _info "ensuring dev tenant topic exists: $DEV_TENANT_TOPIC"
    local out rc
    set +e
    out=$(docker exec -i "$container" rpk topic create "$DEV_TENANT_TOPIC" 2>&1)
    rc=$?
    set -e
    if [ $rc -eq 0 ]; then
        _ok "dev tenant topic created: $DEV_TENANT_TOPIC"
        return 0
    fi
    if printf '%s' "$out" | grep -qiE 'TOPIC_ALREADY_EXISTS|already.*exists'; then
        _ok "dev tenant topic already present: $DEV_TENANT_TOPIC (idempotent)"
        return 0
    fi
    _warn "rpk topic create failed (rc=$rc): $(printf '%s' "$out" | head -c 160)"
    _info "this is non-fatal; the worker pattern subscription will eventually discover the topic"
    return 0
}

# ---------------------------------------------------------------------------- MODE: seed
mode_seed() {
    if [ ! -f "$SEED_FILE" ]; then
        _fail "seed file not found: $SEED_FILE"
        exit 1
    fi
    local container
    if ! container=$(_resolve_pg_container); then
        _fail "could not resolve postgres container — is the stack running?"
        exit 1
    fi
    _info "container: $container"
    _info "applying $SEED_FILE"
    if ! docker exec -i "$container" psql -U "$ZOVARK_PG_USER" -d "$ZOVARK_PG_DB" \
            -v ON_ERROR_STOP=1 -q < "$SEED_FILE"; then
        _fail "psql exited non-zero"
        exit 1
    fi
    _ok "seed applied (or already present — idempotent)"
    # fix-e2e-ingest-stall §2: pre-create the dev tenant Redpanda topic so the
    # worker's pattern-subscribed consumer doesn't have to wait up to one
    # metadata refresh window for first-publish topic discovery. Non-fatal —
    # operators running without the redpanda profile still get a working seed.
    _create_dev_tenant_topic
    exit 0
}

# ---------------------------------------------------------------------------- MODE: check
mode_check() {
    local container
    if ! container=$(_resolve_pg_container); then
        _fail "could not resolve postgres container — is the stack running?"
        exit 1
    fi
    _info "container: $container"

    local tenant_present admin_present analyst_present
    local admin_hash analyst_hash
    local q_tenant q_admin q_analyst

    # Tenant presence
    q_tenant=$(_psql "$container" -c \
        "SELECT 1 FROM tenants WHERE id = '00000000-0000-0000-0000-000000000010';" 2>/dev/null | tr -d '[:space:]' || echo "")
    if [ "$q_tenant" = "1" ]; then
        tenant_present=true
    else
        tenant_present=false
    fi

    # Admin presence + hash cost prefix
    q_admin=$(_psql "$container" -c \
        "SELECT substring(password_hash, 1, 7) FROM users WHERE id = '00000000-0000-0000-0000-000000000020';" 2>/dev/null | tr -d '[:space:]' || echo "")
    if [ -n "$q_admin" ]; then
        admin_present=true
        admin_hash="$q_admin"
    else
        admin_present=false
        admin_hash=""
    fi

    # Analyst presence + hash cost prefix
    q_analyst=$(_psql "$container" -c \
        "SELECT substring(password_hash, 1, 7) FROM users WHERE id = '00000000-0000-0000-0000-000000000021';" 2>/dev/null | tr -d '[:space:]' || echo "")
    if [ -n "$q_analyst" ]; then
        analyst_present=true
        analyst_hash="$q_analyst"
    else
        analyst_present=false
        analyst_hash=""
    fi

    # Render table
    printf '\n%sFixture check%s\n' "$C_BOLD" "$C_RESET"
    printf '%s\n' "----------------------------------------------------------------"
    _render_row() {
        local label="$1" present="$2" extra="$3"
        if [ "$present" = "true" ]; then
            printf '  %s%-42s %s✓%s %s\n' "$C_RESET" "$label" "$C_GREEN" "$C_RESET" "$extra"
        else
            printf '  %s%-42s %s✗%s %s\n' "$C_RESET" "$label" "$C_RED" "$C_RESET" "$extra"
        fi
    }

    local admin_cost_label="$admin_hash"
    if $admin_present; then
        case "$admin_hash" in
            '$2b$12$'|'$2a$12$') admin_cost_label="[cost=12]" ;;
            *) admin_cost_label="[unexpected cost: $admin_hash]" ;;
        esac
    fi
    local analyst_cost_label="$analyst_hash"
    if $analyst_present; then
        case "$analyst_hash" in
            '$2b$12$'|'$2a$12$') analyst_cost_label="[cost=12]" ;;
            *) analyst_cost_label="[unexpected cost: $analyst_hash]" ;;
        esac
    fi

    # fix-e2e-ingest-stall §2: also verify the dev tenant Redpanda topic
    # exists. Non-fatal if redpanda isn't running (e.g. operator skipped that
    # profile) — reports as `skipped` instead of a hard failure.
    local topic_present="false" topic_label=""
    local rp_container
    if rp_container=$(_resolve_redpanda_container); then
        if docker exec -i "$rp_container" rpk topic list 2>/dev/null | grep -qF "$DEV_TENANT_TOPIC"; then
            topic_present="true"
            topic_label="[present]"
        else
            topic_present="false"
            topic_label="[missing — re-run scripts/seed_dev.sh]"
        fi
    else
        topic_present="skip"
        topic_label="[redpanda container not running]"
    fi

    _render_row "tenant zovark-dev (…0010)" "$tenant_present" ""
    _render_row "user admin@test.local (…0020)" "$admin_present" "$admin_cost_label"
    _render_row "user analyst2@test.local (…0021)" "$analyst_present" "$analyst_cost_label"
    if [ "$topic_present" = "skip" ]; then
        printf '  %s%-42s %s-%s %s\n' "$C_RESET" "redpanda topic $DEV_TENANT_TOPIC" "$C_GRAY" "$C_RESET" "$topic_label"
    else
        _render_row "redpanda topic tasks.new.…0010" "$topic_present" "$topic_label"
    fi
    printf '%s\n\n' "----------------------------------------------------------------"

    if ! $tenant_present; then
        _fail "tenant missing — seed never ran. Run: scripts/seed_dev.sh"
        exit 1
    fi
    if $tenant_present && $admin_present && $analyst_present \
        && [ "$admin_hash" = '$2b$12$' -o "$admin_hash" = '$2a$12$' ] \
        && [ "$analyst_hash" = '$2b$12$' -o "$analyst_hash" = '$2a$12$' ]; then
        # Topic absent on a stack with redpanda running is a soft warning,
        # not a hard fail — the consumer's metadata refresh will still
        # discover it on the first publish (just slower).
        if [ "$topic_present" = "false" ]; then
            _warn "all DB fixtures present but dev tenant topic missing — first publish may be slow"
            exit 2
        fi
        _ok "all fixtures present with bcrypt cost 12"
        exit 0
    fi
    _warn "partial fixture state — re-run: scripts/seed_dev.sh"
    exit 2
}

# ---------------------------------------------------------------------------- MODE: regenerate
mode_regenerate() {
    if ! command -v python3 >/dev/null 2>&1; then
        _fail "python3 is required for --regenerate-hash"
        exit 2
    fi
    if ! python3 -c 'import bcrypt' >/dev/null 2>&1; then
        _fail "python3 bcrypt package is required for --regenerate-hash"
        _info "install: pip install bcrypt"
        exit 2
    fi
    local new_hash
    new_hash=$(python3 -c \
        'import bcrypt; print(bcrypt.hashpw(b"TestPass2026", bcrypt.gensalt(12)).decode())')
    if [ -z "$new_hash" ] || [[ ! "$new_hash" =~ ^\$2[ab]\$12\$ ]]; then
        _fail "bcrypt produced an unexpected hash: $new_hash"
        exit 1
    fi

    cat <<EOF

-- Rotation SQL — paste into psql to update the RUNNING database:
UPDATE users
   SET password_hash = '$new_hash',
       updated_at = NOW()
 WHERE email IN ('admin@test.local', 'analyst2@test.local');

-- THEN also update the literal hash in migrations/seed_dev_data.sql so
-- fresh boots pick up the new value. Search for the existing \$2b\$12\$ line
-- and replace with:
--   '$new_hash'

EOF
    exit 0
}

# ---------------------------------------------------------------------------- Main
case "$MODE" in
    seed) mode_seed ;;
    check) mode_check ;;
    regenerate) mode_regenerate ;;
    *) _fail "unknown mode: $MODE"; exit 2 ;;
esac
