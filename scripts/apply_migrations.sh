#!/usr/bin/env bash
# Apply PostgreSQL migrations from migrations/*.sql, ledger-aware.
#
# This script is the canonical operator workflow for bringing a Zovark Postgres
# DB current with the on-disk migrations/ directory. It records every applied
# file in the schema_migrations ledger (created by migrations/072_…) so re-runs
# are idempotent and operators can see exactly what's been applied.
#
# ── Default behaviour (no flags): ──────────────────────────────────────────
#   1. Discover migration files in migrations/*.sql in numeric-then-lex order.
#   2. If schema_migrations doesn't exist yet, special-case migration 072 to
#      bootstrap the ledger (migration 072 itself creates the table AND
#      backfills the init.sql era).
#   3. For each file not yet in the ledger, apply it inside an isolated
#      transaction and INSERT a row into schema_migrations on success.
#   4. Skip migration 068 (SurrealDB cutover gate) unless --include-068.
#
# ── Two-phase apply (Ticket 2 / SurrealDB cutover) ─────────────────────────
#   Phase 1 (safe default): everything except 068 — runs cleanly on dev volumes.
#     068_ticket2_surreal_graph_pgvector_retirement.sql drops PostgreSQL entity
#     graph / pgvector paths and MUST run only after SurrealDB is live and the
#     entity write path is migrated.
#   Phase 2 (explicit opt-in): after cutover, re-run with --include-068
#     (prompts for confirmation; type APPLY-068).
#
# Usage:
#   scripts/apply_migrations.sh                      # apply all pending (sans 068)
#   scripts/apply_migrations.sh --dry-run            # print to-apply list, exit 0
#   scripts/apply_migrations.sh --include-068        # phase 2 (after cutover)
#   scripts/apply_migrations.sh --from 040 --to 069  # legacy range mode
#   scripts/apply_migrations.sh --mark-applied <file> <source>
#                                                    # backfill a manually-run file
#                                                    # source ∈ {init_sql, migration_runner, manual_backfill}
#   scripts/apply_migrations.sh psql                 # legacy alias for default
#   scripts/apply_migrations.sh api                  # delegate to ./hydra-api migrate up
#
# Env:
#   API_SERVICE         Compose service name for api mode (default: api)
#   POSTGRES_SERVICE    default: postgres
#   POSTGRES_USER       default: zovark
#   POSTGRES_DB         default: zovark
#   PGPASSWORD          optional; forwarded into the postgres container for psql
#   COMPOSE_FILE        optional (same as docker compose -f)
#
# See: docs/RUNBOOK_HEALTHCHECK.md#schema-drift

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FROM="000"
TO="999"
MODE="psql"
INCLUDE_068=0
DRY_RUN=0
MARK_FILE=""
MARK_SOURCE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from)
      FROM="${2:?}"
      shift 2
      ;;
    --to)
      TO="${2:?}"
      shift 2
      ;;
    --include-068)
      INCLUDE_068=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --mark-applied)
      if [[ $# -lt 3 ]]; then
        echo "ERROR: --mark-applied requires <filename> <source>" >&2
        exit 2
      fi
      MARK_FILE="$2"
      MARK_SOURCE="$3"
      shift 3
      ;;
    psql|api)
      MODE="$1"
      shift
      ;;
    -h|--help)
      sed -n '1,55p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

API_SERVICE="${API_SERVICE:-api}"

if [[ "$MODE" == "api" ]]; then
  exec docker compose exec -T "$API_SERVICE" ./hydra-api migrate up
fi

if (( 10#$FROM == 68 && 10#$TO == 68 )) && [[ "$INCLUDE_068" != "1" ]]; then
  echo "Error: range is only 068; add --include-068 after SurrealDB + entity write cutover." >&2
  exit 1
fi

POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgres}"
POSTGRES_USER="${POSTGRES_USER:-zovark}"
POSTGRES_DB="${POSTGRES_DB:-zovark}"

exec_args=(-T)
if [[ -n "${PGPASSWORD:-}" ]]; then
  exec_args+=(-e "PGPASSWORD=${PGPASSWORD}")
fi

# ── Helpers ────────────────────────────────────────────────────────────────
_psql_quiet() {
  docker compose exec "${exec_args[@]}" "$POSTGRES_SERVICE" \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA "$@"
}

ledger_exists() {
  local out
  out=$(_psql_quiet -c "SELECT to_regclass('public.schema_migrations')" 2>/dev/null || echo "")
  out="$(echo "$out" | tr -d '[:space:]')"
  [[ -n "$out" && "$out" != "" ]]
}

is_applied() {
  local fname="$1"
  if ! ledger_exists; then
    return 1
  fi
  local got
  got=$(_psql_quiet -c "SELECT 1 FROM schema_migrations WHERE filename = '$fname'" 2>/dev/null | tr -d '[:space:]' || echo "")
  [[ "$got" = "1" ]]
}

sha256_of() {
  sha256sum "$ROOT/migrations/$1" | awk '{print $1}'
}

# Apply a migration file inside its own transaction and INSERT a ledger row on
# success. On psql failure the transaction rolls back and the script exits 1.
apply_one() {
  local fname="$1"
  local checksum
  checksum=$(sha256_of "$fname")
  local applied_by="apply_migrations.sh@$(hostname 2>/dev/null || echo unknown)"

  echo "applying $fname ..."
  local stderr_file
  stderr_file=$(mktemp)
  if ! docker compose exec "${exec_args[@]}" "$POSTGRES_SERVICE" \
        psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        -v ON_ERROR_STOP=1 \
        --single-transaction \
        -c "\\set fname '$fname'" \
        -c "\\set checksum '$checksum'" \
        -c "\\set applied_by '$applied_by'" \
        -f - < "$ROOT/migrations/$fname" 2> "$stderr_file"; then
    echo "FAILED: $fname" >&2
    echo "----- last 20 lines of psql stderr -----" >&2
    tail -20 "$stderr_file" >&2
    echo "-----------------------------------------" >&2
    rm -f "$stderr_file"
    exit 1
  fi
  rm -f "$stderr_file"

  # Record in ledger AFTER the migration's transaction has committed. Two-step
  # so a migration that contains its own COMMIT (like 072) doesn't conflict.
  _psql_quiet -c "INSERT INTO schema_migrations (filename, applied_by, source, checksum) VALUES ('$fname', '$applied_by', 'migration_runner', '$checksum') ON CONFLICT (filename) DO NOTHING" >/dev/null
}

# ── --mark-applied ─────────────────────────────────────────────────────────
if [[ -n "$MARK_FILE" ]]; then
  case "$MARK_SOURCE" in
    init_sql|migration_runner|manual_backfill) ;;
    *)
      echo "ERROR: source must be one of: init_sql, migration_runner, manual_backfill (got: $MARK_SOURCE)" >&2
      exit 2
      ;;
  esac
  if [[ ! -f "$ROOT/migrations/$MARK_FILE" ]]; then
    echo "ERROR: file does not exist: migrations/$MARK_FILE" >&2
    exit 1
  fi
  if ! ledger_exists; then
    echo "ERROR: schema_migrations table does not exist — apply migration 072 first via 'scripts/apply_migrations.sh' (no flag)" >&2
    exit 1
  fi
  checksum=$(sha256_of "$MARK_FILE")
  applied_by="apply_migrations.sh@$(hostname 2>/dev/null || echo unknown) (--mark-applied)"
  _psql_quiet -c "INSERT INTO schema_migrations (filename, applied_by, source, checksum) VALUES ('$MARK_FILE', '$applied_by', '$MARK_SOURCE', '$checksum') ON CONFLICT (filename) DO NOTHING" >/dev/null
  echo "marked $MARK_FILE as $MARK_SOURCE"
  exit 0
fi

# ── api mode delegate (legacy) ─────────────────────────────────────────────
if [[ "$MODE" == "api" ]]; then
  exec docker compose exec -T "${API_SERVICE:-api}" ./hydra-api migrate up
fi

if (( 10#$FROM == 68 && 10#$TO == 68 )) && [[ "$INCLUDE_068" != "1" ]]; then
  echo "Error: range is only 068; add --include-068 after SurrealDB + entity write cutover." >&2
  exit 1
fi

shopt -s nullglob
all_files=("$ROOT/migrations/"*.sql)
if [[ ${#all_files[@]} -eq 0 ]]; then
  echo "No migrations found under $ROOT/migrations/" >&2
  exit 1
fi

if (( 10#$FROM <= 68 && 10#$TO >= 68 )) && [[ "$INCLUDE_068" != "1" ]]; then
  echo "Note: migration 068 is excluded by default (SurrealDB cutover). Use --include-068 after entity graph is on SurrealDB." >&2
fi

# ── Build to-apply list ────────────────────────────────────────────────────
ledger_present=true
if ! ledger_exists; then
  ledger_present=false
fi

to_apply=()
for f in $(printf '%s\n' "${all_files[@]}" | sort -V); do
  base="$(basename "$f")"
  [[ "$base" == seed_* ]] && continue
  num="${base:0:3}"
  if ! [[ "$num" =~ ^[0-9]{3}$ ]]; then
    echo "Skip (unrecognized prefix): $base" >&2
    continue
  fi
  if (( 10#$num < 10#$FROM || 10#$num > 10#$TO )); then
    continue
  fi
  if (( 10#$num == 68 )) && [[ "$INCLUDE_068" != "1" ]]; then
    continue
  fi
  if $ledger_present && is_applied "$base"; then
    continue
  fi
  # Special case: when the ledger is absent, only 072 can be in the initial
  # to-apply list. Everything else has to wait for the bootstrap pass.
  if ! $ledger_present && [[ "$base" != "072_schema_migrations_ledger.sql" ]]; then
    continue
  fi
  to_apply+=("$base")
done

# ── --dry-run ──────────────────────────────────────────────────────────────
if (( DRY_RUN == 1 )); then
  if $ledger_present; then
    echo "ledger present; ${#to_apply[@]} migration(s) to apply:"
  else
    echo "ledger absent; would bootstrap with 072 then re-scan."
    echo "${#to_apply[@]} migration(s) in initial scan:"
  fi
  for f in "${to_apply[@]}"; do
    echo "  - $f"
  done
  if ! $ledger_present; then
    echo "(after bootstrap, additional files numbered ≥054 not yet in ledger will be in scope.)" >&2
  fi
  exit 0
fi

# ── Wet run ────────────────────────────────────────────────────────────────
applied_count=0

for base in "${to_apply[@]}"; do
  # 068 interactive confirmation gate.
  num="${base:0:3}"
  if (( 10#$num == 68 )) && [[ -z "${ZOVARK_068_CONFIRMED:-}" ]]; then
    echo "" >&2
    echo "You are about to apply $base — PostgreSQL entity graph / pgvector retirement." >&2
    echo "Confirm SurrealDB is live and entity writes are migrated. Type APPLY-068 to continue:" >&2
    read -r _confirm
    if [[ "$_confirm" != "APPLY-068" ]]; then
      echo "Aborted (expected APPLY-068)." >&2
      exit 1
    fi
    export ZOVARK_068_CONFIRMED=1
  fi

  apply_one "$base"
  applied_count=$(( applied_count + 1 ))
done

# Bootstrap re-scan: if we just applied 072, the ledger now exists, so loop
# again to pick up everything that was previously gated.
if ! $ledger_present; then
  ledger_present=true
  to_apply=()
  for f in $(printf '%s\n' "${all_files[@]}" | sort -V); do
    base="$(basename "$f")"
    [[ "$base" == seed_* ]] && continue
    num="${base:0:3}"
    [[ "$num" =~ ^[0-9]{3}$ ]] || continue
    if (( 10#$num < 10#$FROM || 10#$num > 10#$TO )); then continue; fi
    if (( 10#$num == 68 )) && [[ "$INCLUDE_068" != "1" ]]; then continue; fi
    if is_applied "$base"; then continue; fi
    to_apply+=("$base")
  done
  for base in "${to_apply[@]}"; do
    num="${base:0:3}"
    if (( 10#$num == 68 )) && [[ -z "${ZOVARK_068_CONFIRMED:-}" ]]; then
      echo "" >&2
      echo "You are about to apply $base — PostgreSQL entity graph / pgvector retirement." >&2
      echo "Confirm SurrealDB is live and entity writes are migrated. Type APPLY-068 to continue:" >&2
      read -r _confirm
      if [[ "$_confirm" != "APPLY-068" ]]; then
        echo "Aborted (expected APPLY-068)." >&2
        exit 1
      fi
      export ZOVARK_068_CONFIRMED=1
    fi
    apply_one "$base"
    applied_count=$(( applied_count + 1 ))
  done
fi

ledger_total=$(_psql_quiet -c "SELECT count(*) FROM schema_migrations" 2>/dev/null | tr -d '[:space:]' || echo "?")
echo "applied $applied_count migration(s); ledger now has $ledger_total row(s)"
exit 0
