#!/usr/bin/env bash
# ============================================================================
# git_sync.sh — rebase current feature branch on upstream/master and push
#               with --force-with-lease.
#
# Usage: scripts/git_sync.sh [--no-color] [--quiet] [--help]
#
# Steps:
#   1. Refuse to run on the base branch.
#   2. Fetch upstream.
#   3. If already up-to-date, exit 2 (no-op).
#   4. Rebase current branch onto <upstream>/<base>.
#   5. Push to origin with --force-with-lease.
#
# On rebase conflict: prints the conflict count, leaves the repo in the
# rebase-in-progress state, exits 1. Does NOT call `git rebase --abort`.
#
# Exit codes:
#   0  rebased and pushed
#   1  hard failure (refuse-to-rebase-master, fetch failed, rebase conflict,
#      force-with-lease rejected, etc.)
#   2  already up to date (no-op)
#
# Env:
#   ZOVARK_UPSTREAM_REMOTE  default "upstream"
#   ZOVARK_ORIGIN_REMOTE    default "origin"
#   ZOVARK_BASE_BRANCH      default "master"
# ============================================================================

set -euo pipefail
MSYS_NO_PATHCONV=1

_SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
if [ ! -f "$_SCRIPT_DIR/lib/git_common.sh" ]; then
    echo "git_sync: missing scripts/lib/git_common.sh" >&2
    exit 1
fi
# shellcheck source=lib/git_common.sh
source "$_SCRIPT_DIR/lib/git_common.sh"

usage() {
    cat <<'EOF'
Usage: scripts/git_sync.sh [OPTIONS]

Rebase the current feature branch on top of upstream/master and push with
--force-with-lease. Refuses to run on the base branch.

OPTIONS:
  --no-color      Suppress ANSI color escapes.
  --quiet, -q     Suppress step-by-step narration (errors still print).
  --help, -h      Show this help and exit 0.

ENV VARS:
  ZOVARK_UPSTREAM_REMOTE    default "upstream"
  ZOVARK_ORIGIN_REMOTE      default "origin"
  ZOVARK_BASE_BRANCH        default "master"

EXIT CODES:
  0   successfully rebased and pushed
  1   hard failure (refuse-master, conflict, push rejected, ...)
  2   already up to date (no rebase needed)

SAFETY:
  - NEVER uses `git push --force` — only `--force-with-lease`.
  - NEVER calls `git rebase --abort` on your behalf.
  - NEVER runs on the base branch.

EXAMPLES:
  scripts/git_sync.sh
  scripts/git_sync.sh --quiet
EOF
}

_parse_common_flags "$@"
set -- ${REMAINING_ARGS[@]+"${REMAINING_ARGS[@]}"}

if [ "$WANT_HELP" = "1" ]; then
    usage
    exit 0
fi

if [ $# -gt 0 ]; then
    _emit_fail "git_sync: unexpected positional arguments: $*"
    usage >&2
    exit 1
fi

_env_defaults
_require_git_repo

CURRENT_BRANCH="$(_current_branch)"
if [ -z "$CURRENT_BRANCH" ] || [ "$CURRENT_BRANCH" = "HEAD" ]; then
    _emit_fail "git_sync: detached HEAD — cannot rebase"
    exit 1
fi
if [ "$CURRENT_BRANCH" = "$ZOVARK_BASE_BRANCH" ]; then
    _emit_fail "git_sync: refusing to rebase '$ZOVARK_BASE_BRANCH' on itself"
    _emit_info "create a feature branch first: scripts/git_fresh.sh <name>"
    exit 1
fi

_emit_info "current branch: $CURRENT_BRANCH"
_emit_info "git fetch $ZOVARK_UPSTREAM_REMOTE $ZOVARK_BASE_BRANCH"
if ! git fetch "$ZOVARK_UPSTREAM_REMOTE" "$ZOVARK_BASE_BRANCH"; then
    _emit_fail "git fetch failed"
    exit 1
fi

# No-op detection: if upstream/base is already an ancestor of HEAD, nothing to do.
if git merge-base --is-ancestor "$ZOVARK_UPSTREAM_REMOTE/$ZOVARK_BASE_BRANCH" HEAD 2>/dev/null; then
    _emit_ok "already up to date with $ZOVARK_UPSTREAM_REMOTE/$ZOVARK_BASE_BRANCH — nothing to rebase"
    exit 2
fi

_emit_info "git rebase $ZOVARK_UPSTREAM_REMOTE/$ZOVARK_BASE_BRANCH"
REBASE_OUT=""
set +e
REBASE_OUT=$(git rebase "$ZOVARK_UPSTREAM_REMOTE/$ZOVARK_BASE_BRANCH" 2>&1)
REBASE_RC=$?
set -e

if [ $REBASE_RC -ne 0 ]; then
    CONFLICT_COUNT=$(git diff --name-only --diff-filter=U 2>/dev/null | wc -l | tr -d ' ')
    [ -z "$CONFLICT_COUNT" ] && CONFLICT_COUNT=0
    _emit_fail "git rebase failed with $CONFLICT_COUNT file(s) in conflict"
    if [ "$CONFLICT_COUNT" -gt 0 ]; then
        _emit_info "conflicting files:"
        git diff --name-only --diff-filter=U | sed 's/^/  /' >&2 || true
    fi
    _emit_info "after fixing, resume with: git rebase --continue"
    _emit_info "to give up, run manually:  git rebase --abort"
    _emit_info "(git_sync will NOT abort for you)"
    # First ~5 lines of rebase stderr as context.
    if [ -n "$REBASE_OUT" ]; then
        printf '%s\n' "$REBASE_OUT" | head -n 5 >&2 || true
    fi
    exit 1
fi

NEW_SHA=$(git rev-parse --short HEAD)
_emit_ok "rebase clean — HEAD now at $NEW_SHA"

_emit_info "git push --force-with-lease $ZOVARK_ORIGIN_REMOTE HEAD"
if ! git push --force-with-lease "$ZOVARK_ORIGIN_REMOTE" HEAD; then
    _emit_fail "force-with-lease push rejected — somebody else may have pushed to $CURRENT_BRANCH"
    _emit_info "investigate with: git fetch $ZOVARK_ORIGIN_REMOTE $CURRENT_BRANCH && git log HEAD..$ZOVARK_ORIGIN_REMOTE/$CURRENT_BRANCH"
    exit 1
fi

_emit_ok "branch $CURRENT_BRANCH synced with $ZOVARK_UPSTREAM_REMOTE/$ZOVARK_BASE_BRANCH and pushed"
exit 0
