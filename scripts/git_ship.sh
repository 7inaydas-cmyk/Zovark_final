#!/usr/bin/env bash
# ============================================================================
# git_ship.sh — commit, push, and print the PR compare URL.
#
# Usage: scripts/git_ship.sh [OPTIONS] '<conventional-commit message>'
#
# Behavior:
#   1. If on the base branch, derive <type>/<slug> from the commit message,
#      uniquify against origin, and `git checkout -b` it first.
#   2. If --stage-all, run `git add -A`. Otherwise respect whatever the user
#      has staged manually.
#   3. Run scoped precommit:
#        - Any staged/modified *.go → `(cd api && go build ./...)`
#        - Every staged/modified *.py → `python3 -m py_compile <file>`
#        - ZOVARK_SKIP_PRECOMMIT=1 bypasses all checks.
#        - ZOVARK_PRECOMMIT_FULL=1 runs the full tree, not just the diff.
#   4. `git commit -m <message>` (pre-commit hooks run, never --no-verify).
#   5. `git push -u origin HEAD`.
#   6. Print the PR compare URL.
#
# Exit codes:
#   0   committed and pushed
#   1   hard failure (build error, push rejected, rejected message, ...)
#   2   degraded / no-op (nothing staged to commit; URL still printed)
#
# Safety guarantees:
#   - Never uses `git push --force`.
#   - Never uses `--no-verify`.
#   - Never pushes to `upstream`.
#   - Never amends an already-pushed commit.
#
# Env:
#   ZOVARK_UPSTREAM_REMOTE         default "upstream"
#   ZOVARK_ORIGIN_REMOTE           default "origin"
#   ZOVARK_BASE_BRANCH             default "master"
#   ZOVARK_PR_COMPARE_URL_TEMPLATE optional override
#   ZOVARK_SKIP_PRECOMMIT=1        bypass build + py_compile
#   ZOVARK_PRECOMMIT_FULL=1        run full-tree checks
# ============================================================================

set -euo pipefail
MSYS_NO_PATHCONV=1

_SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
if [ ! -f "$_SCRIPT_DIR/lib/git_common.sh" ]; then
    echo "git_ship: missing scripts/lib/git_common.sh" >&2
    exit 1
fi
# shellcheck source=lib/git_common.sh
source "$_SCRIPT_DIR/lib/git_common.sh"

STAGE_ALL=0

usage() {
    cat <<'EOF'
Usage: scripts/git_ship.sh [OPTIONS] '<conventional-commit message>'

Commit, push, and print the PR compare URL. If run on the base branch, a new
feature branch is auto-created from <type>/<slug> derived from the message.

OPTIONS:
  --stage-all     Run `git add -A` before committing (default: commit only
                  what is already staged).
  --no-color      Suppress ANSI color escapes.
  --quiet, -q     Suppress step-by-step narration (errors still print).
  --help, -h      Show this help and exit 0.

ENV VARS:
  ZOVARK_UPSTREAM_REMOTE            default "upstream"
  ZOVARK_ORIGIN_REMOTE              default "origin"
  ZOVARK_BASE_BRANCH                default "master"
  ZOVARK_PR_COMPARE_URL_TEMPLATE    optional template override
  ZOVARK_SKIP_PRECOMMIT=1           bypass go build + py_compile
  ZOVARK_PRECOMMIT_FULL=1           run full-tree precommit (vs diff-only)

ACCEPTED COMMIT TYPES:
  feat, fix, chore, docs, refactor, test, perf, ci, build, style, revert

EXIT CODES:
  0   committed and pushed
  1   hard failure
  2   degraded / no-op — nothing staged to commit (URL still printed)

EXAMPLES:
  scripts/git_ship.sh 'fix: dashboard SSE ticket leak'
  scripts/git_ship.sh --stage-all 'feat(api): add /v1/audit export'
  ZOVARK_SKIP_PRECOMMIT=1 scripts/git_ship.sh 'chore: rollback hotfix'
EOF
}

# Pre-scan for --stage-all (keeps _parse_common_flags simple).
_FILTERED=()
for arg in "$@"; do
    case "$arg" in
        --stage-all) STAGE_ALL=1 ;;
        *) _FILTERED+=("$arg") ;;
    esac
done
set -- ${_FILTERED[@]+"${_FILTERED[@]}"}

_parse_common_flags "$@"
set -- ${REMAINING_ARGS[@]+"${REMAINING_ARGS[@]}"}

if [ "$WANT_HELP" = "1" ]; then
    usage
    exit 0
fi

if [ $# -ne 1 ] || [ -z "${1:-}" ]; then
    _emit_fail "git_ship: exactly one positional argument (commit message) required"
    usage >&2
    exit 1
fi

MESSAGE="$1"
_env_defaults
_require_git_repo

CURRENT_BRANCH="$(_current_branch)"
if [ -z "$CURRENT_BRANCH" ] || [ "$CURRENT_BRANCH" = "HEAD" ]; then
    _emit_fail "git_ship: detached HEAD — cannot commit"
    exit 1
fi

# ------------------------------------------------------- Auto-create branch on master
if [ "$CURRENT_BRANCH" = "$ZOVARK_BASE_BRANCH" ]; then
    _emit_info "on $ZOVARK_BASE_BRANCH — deriving feature branch from message"
    set +e
    DERIVED=$(_derive_branch_from_message "$MESSAGE")
    DRC=$?
    set -e
    if [ $DRC -ne 0 ] || [ -z "$DERIVED" ]; then
        # _derive_branch_from_message already printed the hint.
        exit 1
    fi
    set +e
    BRANCH=$(_uniquify_branch_name "$DERIVED")
    URC=$?
    set -e
    if [ $URC -ne 0 ] || [ -z "$BRANCH" ]; then
        exit 1
    fi
    if [ "$BRANCH" != "$DERIVED" ]; then
        _emit_warn "remote branch $DERIVED already exists; using $BRANCH"
    fi
    _emit_info "git checkout -b $BRANCH"
    if ! git checkout -b "$BRANCH"; then
        _emit_fail "git checkout -b $BRANCH failed"
        exit 1
    fi
    CURRENT_BRANCH="$BRANCH"
fi

# ------------------------------------------------------- Optional auto-stage
if [ "$STAGE_ALL" = "1" ]; then
    _emit_info "git add -A (stage-all)"
    git add -A
fi

# ------------------------------------------------------- Precommit
run_precommit() {
    if [ "${ZOVARK_SKIP_PRECOMMIT:-0}" = "1" ]; then
        _emit_warn "ZOVARK_SKIP_PRECOMMIT=1 set — skipping build + py_compile checks"
        return 0
    fi

    local changed go_changed py_files
    changed=$(_changed_files || true)

    if [ "${ZOVARK_PRECOMMIT_FULL:-0}" = "1" ]; then
        _emit_info "precommit: ZOVARK_PRECOMMIT_FULL=1 — running full-tree checks"
        go_changed="yes"
        # collect every tracked *.py
        py_files=$(git ls-files '*.py' 2>/dev/null || true)
    else
        if printf '%s\n' "$changed" | grep -qE '\.go$'; then
            go_changed="yes"
        else
            go_changed="no"
        fi
        py_files=$(printf '%s\n' "$changed" | grep -E '\.py$' || true)
    fi

    if [ "$go_changed" = "no" ] && [ -z "$py_files" ]; then
        _emit_info "precommit: no go/py changes in diff, skipping"
        return 0
    fi

    if [ "$go_changed" = "yes" ]; then
        if [ -d "$_SCRIPT_DIR/../api" ]; then
            _emit_info "precommit: go build ./... (inside api/)"
            if ! (cd "$_SCRIPT_DIR/../api" && go build ./...); then
                _emit_fail "go build failed — commit aborted"
                return 1
            fi
        else
            _emit_warn "precommit: api/ dir not found, skipping go build"
        fi
    fi

    if [ -n "$py_files" ]; then
        _emit_info "precommit: python3 -m py_compile on $(printf '%s\n' "$py_files" | wc -l | tr -d ' ') file(s)"
        # py_compile each file individually so errors name the offender.
        while IFS= read -r f; do
            [ -z "$f" ] && continue
            [ -f "$f" ] || continue
            if ! python3 -m py_compile "$f"; then
                _emit_fail "py_compile failed: $f"
                return 1
            fi
        done <<<"$py_files"
    fi

    _emit_ok "precommit: clean"
}

if ! run_precommit; then
    exit 1
fi

# ------------------------------------------------------- URL helper (used by both paths)
_print_pr_url() {
    local url
    url=$(_compare_url "$ZOVARK_UPSTREAM_REMOTE" "$ZOVARK_ORIGIN_REMOTE" "$ZOVARK_BASE_BRANCH" "$CURRENT_BRANCH")
    _emit_ok "PR compare URL:"
    printf '  %s\n' "$url"
}

# ------------------------------------------------------- Nothing staged? degraded exit
if git diff --cached --quiet 2>/dev/null; then
    _emit_warn "nothing staged to commit — re-run with --stage-all or stage manually"
    _print_pr_url
    exit 2
fi

# ------------------------------------------------------- Commit
_emit_info "git commit -m ..."
if ! git commit -m "$MESSAGE"; then
    _emit_fail "git commit failed (pre-commit hook?)"
    exit 1
fi

# ------------------------------------------------------- Push
_emit_info "git push -u $ZOVARK_ORIGIN_REMOTE HEAD"
if ! git push -u "$ZOVARK_ORIGIN_REMOTE" HEAD; then
    _emit_fail "git push rejected"
    exit 1
fi

_emit_ok "commit pushed to $ZOVARK_ORIGIN_REMOTE/$CURRENT_BRANCH"
_print_pr_url
exit 0
