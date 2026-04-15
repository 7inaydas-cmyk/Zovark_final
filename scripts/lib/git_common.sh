# ============================================================================
# scripts/lib/git_common.sh
#
# Shared helpers for scripts/git_ship.sh, scripts/git_sync.sh,
# scripts/git_fresh.sh. This file is meant to be sourced, NOT executed.
#
# Exports:
#   Colors:    C_RESET C_BOLD C_GREEN C_RED C_YELLOW C_GRAY C_CYAN
#   Emit:      _emit_info _emit_ok _emit_warn _emit_fail
#   Guards:    _require_git_repo
#   Git:       _current_branch _has_uncommitted_changes _changed_files
#   Remotes:   _parse_github_remote _compare_url
#   Env:       _env_defaults
#   Flags:     _parse_common_flags       (sets QUIET, NO_COLOR, REMAINING_ARGS)
#   Branch:    _derive_branch_from_message _uniquify_branch_name
#
# Honored env vars (loaded by _env_defaults):
#   ZOVARK_UPSTREAM_REMOTE   default "upstream"
#   ZOVARK_ORIGIN_REMOTE     default "origin"
#   ZOVARK_BASE_BRANCH       default "master"
#   ZOVARK_PR_COMPARE_URL_TEMPLATE   optional override
# ============================================================================

# Guard: this file must be sourced, not executed.
if [ -z "${BASH_VERSION:-}" ]; then
    echo "git_common.sh must be sourced from bash" >&2
    return 1 2>/dev/null || exit 1
fi

# ---------------------------------------------------------------------------- Color setup
# GIT_COMMON_NO_COLOR is set to 1 by the top-level scripts after --no-color is
# parsed, or via _parse_common_flags.
_git_common_init_colors() {
    if [ "${GIT_COMMON_NO_COLOR:-0}" = "0" ] && [ -t 1 ]; then
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
}
_git_common_init_colors

# ---------------------------------------------------------------------------- Emit helpers
_now_hhmm() { date +%H:%M:%S; }

_emit_info() {
    [ "${QUIET:-0}" = "1" ] && return 0
    printf '%s[%s] %sINFO%s  %s\n' \
        "$C_GRAY" "$(_now_hhmm)" "$C_CYAN" "$C_RESET" "$*"
}

_emit_ok() {
    printf '%s[%s] %sOK%s    %s\n' \
        "$C_GRAY" "$(_now_hhmm)" "$C_GREEN" "$C_RESET" "$*"
}

_emit_warn() {
    printf '%s[%s] %sWARN%s  %s\n' \
        "$C_GRAY" "$(_now_hhmm)" "$C_YELLOW" "$C_RESET" "$*" >&2
}

_emit_fail() {
    printf '%s[%s] %sFAIL%s  %s\n' \
        "$C_GRAY" "$(_now_hhmm)" "$C_RED" "$C_RESET" "$*" >&2
}

# ---------------------------------------------------------------------------- Git guards
_require_git_repo() {
    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        _emit_fail "not inside a git repository"
        exit 1
    fi
}

_current_branch() {
    git rev-parse --abbrev-ref HEAD 2>/dev/null
}

_has_uncommitted_changes() {
    # 0 = dirty (there is something uncommitted or untracked)
    # 1 = clean
    if [ -z "$(git status --porcelain 2>/dev/null)" ]; then
        return 1
    fi
    return 0
}

_changed_files() {
    # Print tracked-modified + tracked-added + staged files. Excludes untracked
    # (status "??") so that we don't run precommit on files the user hasn't
    # intentionally staged.
    git status --porcelain 2>/dev/null \
        | awk '
            /^\?\?/ { next }
            { print substr($0, 4) }
          '
}

# ---------------------------------------------------------------------------- Remote parsing
# Parse `git remote get-url <remote>` into `org/repo`. Handles:
#   https://github.com/org/repo.git
#   https://github.com/org/repo
#   git@github.com:org/repo.git
#   git@github.com:org/repo
#   ssh://git@github.com/org/repo.git
_parse_github_remote() {
    local remote="$1"
    local url
    if ! url=$(git remote get-url "$remote" 2>/dev/null); then
        return 1
    fi
    local slug=""
    case "$url" in
        git@github.com:*)
            slug="${url#git@github.com:}" ;;
        ssh://git@github.com/*)
            slug="${url#ssh://git@github.com/}" ;;
        https://github.com/*)
            slug="${url#https://github.com/}" ;;
        http://github.com/*)
            slug="${url#http://github.com/}" ;;
        *)
            return 1 ;;
    esac
    slug="${slug%.git}"
    # Must look like org/repo with no trailing slashes
    case "$slug" in
        */*) printf '%s' "$slug"; return 0 ;;
        *)   return 1 ;;
    esac
}

# Build the PR compare URL. Args: <upstream-remote> <origin-remote> <base> <head>
# Falls back to hardcoded swami086/7inaydas-cmyk default with a warning.
_compare_url() {
    local upstream_remote="$1"
    local origin_remote="$2"
    local base="$3"
    local head="$4"

    # Honour explicit template override first.
    if [ -n "${ZOVARK_PR_COMPARE_URL_TEMPLATE:-}" ]; then
        local tpl="$ZOVARK_PR_COMPARE_URL_TEMPLATE"
        tpl="${tpl//<base>/$base}"
        tpl="${tpl//<head>/$head}"
        tpl="${tpl//<branch>/$head}"
        printf '%s' "$tpl"
        return 0
    fi

    local upstream_slug origin_slug
    if upstream_slug=$(_parse_github_remote "$upstream_remote") \
        && origin_slug=$(_parse_github_remote "$origin_remote"); then
        local upstream_org="${upstream_slug%%/*}"
        local upstream_repo="${upstream_slug##*/}"
        local origin_org="${origin_slug%%/*}"
        local origin_repo="${origin_slug##*/}"
        printf 'https://github.com/%s/%s/compare/%s...%s:%s:%s' \
            "$upstream_org" "$upstream_repo" "$base" \
            "$origin_org" "$origin_repo" "$head"
        return 0
    fi

    _emit_warn "could not parse github remotes; falling back to hardcoded compare URL"
    printf 'https://github.com/swami086/Zovark_swami/compare/%s...7inaydas-cmyk:Zovark_swami:%s' \
        "$base" "$head"
}

# ---------------------------------------------------------------------------- Env defaults
_env_defaults() {
    export ZOVARK_UPSTREAM_REMOTE="${ZOVARK_UPSTREAM_REMOTE:-upstream}"
    export ZOVARK_ORIGIN_REMOTE="${ZOVARK_ORIGIN_REMOTE:-origin}"
    export ZOVARK_BASE_BRANCH="${ZOVARK_BASE_BRANCH:-master}"
}

# ---------------------------------------------------------------------------- Common flags
# Consumes --no-color, --quiet, --help from the head of the argv. Leaves the
# rest in REMAINING_ARGS. Callers still have to handle --help themselves
# (we set WANT_HELP=1 so they can print usage + exit 0).
_parse_common_flags() {
    REMAINING_ARGS=()
    WANT_HELP=0
    QUIET=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --no-color) GIT_COMMON_NO_COLOR=1; _git_common_init_colors; shift ;;
            --quiet|-q) QUIET=1; shift ;;
            --help|-h)  WANT_HELP=1; shift ;;
            --) shift; REMAINING_ARGS+=("$@"); break ;;
            *)  REMAINING_ARGS+=("$1"); shift ;;
        esac
    done
}

# ---------------------------------------------------------------------------- Branch derivation
# Accepts a conventional-commit message, prints "<type>/<slug>" on stdout.
# Returns non-zero with a hint on stderr if the message isn't parseable.
_derive_branch_from_message() {
    local msg="$1"
    local type=""
    local subject=""

    # Match: type(scope)?: subject — type must be a standard conventional-commit type.
    # Regex is stored in a variable so bash doesn't try to interpret parentheses
    # inside `[[ =~ ]]` as shell grouping.
    local _cc_re='^(feat|fix|chore|docs|refactor|test|perf|ci|build|style|revert)(\([^)]*\))?:[[:space:]]*(.+)$'
    if [[ "$msg" =~ $_cc_re ]]; then
        type="${BASH_REMATCH[1]}"
        subject="${BASH_REMATCH[3]}"
    else
        _emit_fail "commit message must start with one of: feat, fix, chore, docs, refactor, test, perf, ci, build, style, revert"
        _emit_fail "example: scripts/git_ship.sh 'fix: dashboard SSE ticket leak'"
        return 1
    fi

    # Slugify: lowercase, replace non-alnum runs with '-', trim leading/trailing '-',
    # truncate to 48 chars.
    local slug
    slug=$(printf '%s' "$subject" \
        | tr '[:upper:]' '[:lower:]' \
        | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' \
        | cut -c1-48)
    # Re-trim trailing dash after cut (truncation may have landed mid-word).
    slug="${slug%-}"

    if [ -z "$slug" ]; then
        _emit_fail "commit subject produced an empty slug"
        return 1
    fi

    printf '%s/%s' "$type" "$slug"
}

# Append -2, -3, ... to a branch name if it already exists on origin.
_uniquify_branch_name() {
    local base="$1"
    local name="$base"
    local i=2
    while git ls-remote --heads "$ZOVARK_ORIGIN_REMOTE" "$name" 2>/dev/null | grep -q .; do
        name="${base}-${i}"
        i=$((i + 1))
        if [ "$i" -gt 50 ]; then
            _emit_fail "could not find an unused branch name after 50 attempts"
            return 1
        fi
    done
    printf '%s' "$name"
}
