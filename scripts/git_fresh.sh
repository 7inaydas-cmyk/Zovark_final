#!/usr/bin/env bash
# ============================================================================
# git_fresh.sh — create a clean feature branch from a fresh upstream master.
#
# Usage: scripts/git_fresh.sh [--no-color] [--quiet] [--help] <branch-name>
#
# Steps:
#   1. If the working tree is dirty, stash with `git_fresh autostash <ts>`.
#   2. Checkout the base branch (default: master).
#   3. Fast-forward from upstream ($ZOVARK_UPSTREAM_REMOTE/$ZOVARK_BASE_BRANCH).
#   4. Create and checkout the requested branch.
#
# Refuses to run if the requested branch already exists locally.
#
# Exit codes:
#   0  new branch created
#   1  hard failure (existing branch, non-ff pull, checkout error)
#   2  (unused — see git_ship.sh / git_sync.sh)
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
    echo "git_fresh: missing scripts/lib/git_common.sh" >&2
    exit 1
fi
# shellcheck source=lib/git_common.sh
source "$_SCRIPT_DIR/lib/git_common.sh"

usage() {
    cat <<'EOF'
Usage: scripts/git_fresh.sh [OPTIONS] <branch-name>

Create a clean feature branch from a fresh upstream master.

OPTIONS:
  --no-color      Suppress ANSI color escapes.
  --quiet, -q     Suppress step-by-step narration (errors still print).
  --help, -h      Show this help and exit 0.

ENV VARS:
  ZOVARK_UPSTREAM_REMOTE    default "upstream"
  ZOVARK_ORIGIN_REMOTE      default "origin"
  ZOVARK_BASE_BRANCH        default "master"

EXIT CODES:
  0   new branch created and checked out
  1   hard failure (branch already exists, non-ff pull, etc.)

EXAMPLES:
  scripts/git_fresh.sh fix/dashboard-ticket-leak
  scripts/git_fresh.sh --quiet feat/new-endpoint

RECOMMENDED BRANCH NAMING:
  <type>/<slug> where type is one of:
    feat, fix, chore, docs, refactor, test, perf, ci, build, style, revert
EOF
}

_parse_common_flags "$@"
# Empty-array-safe reset: `${arr[@]:-}` inserts a single empty arg when arr is
# empty; use the `+` form instead so $# stays 0 when there are no positionals.
set -- ${REMAINING_ARGS[@]+"${REMAINING_ARGS[@]}"}

if [ "$WANT_HELP" = "1" ]; then
    usage
    exit 0
fi

if [ $# -ne 1 ] || [ -z "${1:-}" ]; then
    _emit_fail "git_fresh: exactly one positional argument (branch name) required"
    usage >&2
    exit 1
fi

BRANCH="$1"
_env_defaults
_require_git_repo

# Refuse if the branch already exists locally. The user explicitly asked for
# a FRESH branch — silently re-using an existing one would be a surprise.
if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
    _emit_fail "git_fresh: local branch '$BRANCH' already exists"
    _emit_info "use a different name or delete the existing branch first"
    exit 1
fi

STASH_NAME=""
if _has_uncommitted_changes; then
    STASH_NAME="git_fresh autostash $(date -u +%Y%m%dT%H%M%SZ)"
    _emit_info "working tree dirty — stashing as: $STASH_NAME"
    if ! git stash push -u -m "$STASH_NAME" >/dev/null; then
        _emit_fail "git stash push failed"
        exit 1
    fi
fi

_emit_info "checkout $ZOVARK_BASE_BRANCH"
if ! git checkout "$ZOVARK_BASE_BRANCH" >/dev/null 2>&1; then
    _emit_fail "git checkout $ZOVARK_BASE_BRANCH failed"
    exit 1
fi

_emit_info "git pull --ff-only $ZOVARK_UPSTREAM_REMOTE $ZOVARK_BASE_BRANCH"
if ! git pull --ff-only "$ZOVARK_UPSTREAM_REMOTE" "$ZOVARK_BASE_BRANCH"; then
    _emit_fail "git pull --ff-only failed — local $ZOVARK_BASE_BRANCH has commits not in upstream"
    _emit_info "resolve by rebasing or resetting local $ZOVARK_BASE_BRANCH, then re-run"
    exit 1
fi

_emit_info "git checkout -b $BRANCH"
if ! git checkout -b "$BRANCH"; then
    _emit_fail "git checkout -b $BRANCH failed"
    exit 1
fi

BASE_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "?")
_emit_ok "created branch $BRANCH at $BASE_SHA"

if [ -n "$STASH_NAME" ]; then
    _emit_warn "your previous uncommitted changes are saved in stash:"
    _emit_warn "  $STASH_NAME"
    _emit_warn "restore with: git stash list | grep 'git_fresh' && git stash pop"
fi

exit 0
