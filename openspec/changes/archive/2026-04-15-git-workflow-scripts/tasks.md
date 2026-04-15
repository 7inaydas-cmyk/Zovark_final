## 1. Shared helper library

- [x] 1.1 Create `scripts/lib/git_common.sh` with shebang guard (`if [ -z "${BASH_VERSION:-}" ]; then return; fi`) and documentation header describing what it exports
- [x] 1.2 Add TTY-aware color variables `C_RESET C_BOLD C_GREEN C_RED C_YELLOW C_GRAY C_CYAN` honoring `--no-color` via a `GIT_COMMON_NO_COLOR` env flag set by the top-level scripts
- [x] 1.3 Add `_emit_info`, `_emit_ok`, `_emit_warn`, `_emit_fail` helpers that print timestamped colored lines to stderr (errors/warnings) or stdout (info/ok)
- [x] 1.4 Add `_require_git_repo` that exits 1 if `git rev-parse --is-inside-work-tree` is not `true`
- [x] 1.5 Add `_current_branch` that prints `git rev-parse --abbrev-ref HEAD`
- [x] 1.6 Add `_has_uncommitted_changes` returning 0 if `git status --porcelain` is non-empty
- [x] 1.7 Add `_changed_files` that prints the list of added+modified files from `git status --porcelain` (handles `??` untracked by NOT including them, matches `git diff --cached --name-only` + unstaged tracked)
- [x] 1.8 Add `_parse_github_remote <remote>` that reads `git remote get-url <remote>`, strips `.git`, converts `git@github.com:org/repo` / `https://github.com/org/repo` to `org/repo`, and prints to stdout — returns non-zero if the URL is not parseable
- [x] 1.9 Add `_compare_url <upstream-ref> <origin-ref> <base> <head>` that constructs the PR compare URL per the design doc, honoring `ZOVARK_PR_COMPARE_URL_TEMPLATE` override with a hardcoded fallback
- [x] 1.10 Add `_env_defaults` helper that exports `ZOVARK_UPSTREAM_REMOTE`, `ZOVARK_ORIGIN_REMOTE`, `ZOVARK_BASE_BRANCH` with their defaults if unset
- [x] 1.11 Add `_parse_common_flags` helper that consumes `--no-color`, `--quiet`, `--help` and leaves the remaining args in `REMAINING_ARGS`
- [x] 1.12 Add `_derive_branch_from_message <message>` that accepts a conventional-commit message and prints `<type>/<slug>`, or exits 1 with the accepted-types hint
- [x] 1.13 Add `_uniquify_branch_name <name>` that appends `-2`, `-3`, … when `git ls-remote --heads origin "<name>"` is non-empty

## 2. git_fresh.sh

- [x] 2.1 Create `scripts/git_fresh.sh` with `#!/usr/bin/env bash`, `set -euo pipefail`, `MSYS_NO_PATHCONV=1`, and `source "$(dirname "$0")/lib/git_common.sh"` (with missing-lib error)
- [x] 2.2 Parse `--no-color`, `--quiet`, `--help` via `_parse_common_flags`; expect exactly one positional argument (the new branch name)
- [x] 2.3 Print usage + exit 0 on `--help`; show the conventional `<type>/<slug>` format hint
- [x] 2.4 Validate the requested branch name does NOT already exist locally (`git show-ref --verify --quiet refs/heads/<branch>`); exit 1 if it does
- [x] 2.5 If `_has_uncommitted_changes` returns true, run `git stash push -u -m "git_fresh autostash $(date -u +%Y%m%dT%H%M%SZ)"` and remember the stash was created so we can tell the user at the end
- [x] 2.6 Run `git checkout "$ZOVARK_BASE_BRANCH"`; fail hard if not on it afterwards
- [x] 2.7 Run `git pull --ff-only "$ZOVARK_UPSTREAM_REMOTE" "$ZOVARK_BASE_BRANCH"`; pass the git error through on failure (exit 1)
- [x] 2.8 Run `git checkout -b "$BRANCH"`; exit 1 on failure
- [x] 2.9 Print final summary: new branch name, upstream head SHA, and if a stash was saved, the stash reference and a `git stash pop` hint
- [x] 2.10 Exit 0 on success

## 3. git_sync.sh

- [x] 3.1 Create `scripts/git_sync.sh` with the same shebang/strict-mode/lib-source header as git_fresh.sh
- [x] 3.2 Parse `--no-color`, `--quiet`, `--help` via `_parse_common_flags`; no positional args
- [x] 3.3 Print usage + exit 0 on `--help`
- [x] 3.4 `_require_git_repo`; exit 1 if the working tree is not a git repo
- [x] 3.5 Read `CURRENT_BRANCH=$(_current_branch)`; if it equals `$ZOVARK_BASE_BRANCH`, exit 1 with the refuse-to-rebase-master message
- [x] 3.6 Run `git fetch "$ZOVARK_UPSTREAM_REMOTE" "$ZOVARK_BASE_BRANCH"`; pass the git error through on failure
- [x] 3.7 Detect no-op case: if `git merge-base --is-ancestor "$ZOVARK_UPSTREAM_REMOTE/$ZOVARK_BASE_BRANCH" HEAD` returns 0, print "already up to date" and exit 2
- [x] 3.8 Run `git rebase "$ZOVARK_UPSTREAM_REMOTE/$ZOVARK_BASE_BRANCH"`; capture stdout/stderr
- [x] 3.9 On rebase failure, compute conflict count via `git diff --name-only --diff-filter=U | wc -l`, print it with `_emit_fail`, print the one-line `git rebase --continue` hint, do NOT call `git rebase --abort`, and exit 1
- [x] 3.10 On rebase success, run `git push --force-with-lease "$ZOVARK_ORIGIN_REMOTE" HEAD`; exit 1 on failure with the git error
- [x] 3.11 Print final summary (new head SHA, number of commits replayed) and exit 0

## 4. git_ship.sh

- [x] 4.1 Create `scripts/git_ship.sh` with the same shebang/strict-mode/lib-source header
- [x] 4.2 Parse `--no-color`, `--quiet`, `--help`, and a new `--stage-all` flag; expect exactly one positional argument (the commit message)
- [x] 4.3 Print usage + exit 0 on `--help`; document all flags and env vars
- [x] 4.4 `_require_git_repo`; exit 1 on failure
- [x] 4.5 If the current branch is `$ZOVARK_BASE_BRANCH`, derive the new branch name via `_derive_branch_from_message "$MESSAGE"`, uniquify via `_uniquify_branch_name`, then `git checkout -b "$BRANCH"`
- [x] 4.6 If `--stage-all` was passed, run `git add -A`
- [x] 4.7 Run precommit checks on `_changed_files`:
        - If any file matches `\.go$`, `(cd api && go build ./...)` unless `ZOVARK_SKIP_PRECOMMIT=1`
        - For every file matching `\.py$`, run `python3 -m py_compile <file>` unless `ZOVARK_SKIP_PRECOMMIT=1`
        - If neither language matches, print "precommit: no go/py changes, skipping"
        - `ZOVARK_PRECOMMIT_FULL=1` widens Go to the whole tree and py_compile to every tracked `*.py`
- [x] 4.8 If the staging area is empty (`git diff --cached --quiet`), print "nothing staged to commit" and jump to the URL-print + exit 2 path
- [x] 4.9 Run `git commit -m "$MESSAGE"` (pre-commit hooks honored — no `--no-verify`)
- [x] 4.10 Run `git push -u "$ZOVARK_ORIGIN_REMOTE" HEAD`; exit 1 on failure
- [x] 4.11 Construct the PR URL via `_compare_url` and print it with `_emit_ok` prefix; also print a copy-paste-friendly line containing only the URL
- [x] 4.12 Exit 0 on successful commit+push; exit 2 when we skipped the commit because the index was empty but still printed the URL

## 5. Docs

- [x] 5.1 Update `CLAUDE.md` with a new "Contributor quickstart" subsection listing the three commands in order (`git_fresh.sh` → edit → `git_ship.sh` → `git_sync.sh`)
- [x] 5.2 Add a one-line pointer under the existing "Scripts" table in `CLAUDE.md` for each of `git_fresh.sh`, `git_ship.sh`, `git_sync.sh`, `lib/git_common.sh`

## 6. Verification

- [x] 6.1 `bash -n scripts/git_fresh.sh && bash -n scripts/git_sync.sh && bash -n scripts/git_ship.sh && bash -n scripts/lib/git_common.sh` all exit 0
- [x] 6.2 `scripts/git_fresh.sh --help`, `scripts/git_sync.sh --help`, `scripts/git_ship.sh --help` each exit 0 and print usage
- [x] 6.3 `scripts/git_ship.sh 'bad message'` on master exits 1 with the accepted-types hint
- [x] 6.4 `test -x scripts/git_fresh.sh && test -x scripts/git_sync.sh && test -x scripts/git_ship.sh` — all executable
- [x] 6.5 `_derive_branch_from_message 'fix: dashboard SSE ticket leak'` → `fix/dashboard-sse-ticket-leak` (unit via `bash -c 'source scripts/lib/git_common.sh; _derive_branch_from_message ...'`)
- [x] 6.6 `_parse_github_remote` handles both HTTPS and `git@github.com:` forms
- [x] 6.7 Dry run on a scratch branch (create a trivial file change, run `scripts/git_ship.sh 'chore: verify'` against origin) — manual, optional
