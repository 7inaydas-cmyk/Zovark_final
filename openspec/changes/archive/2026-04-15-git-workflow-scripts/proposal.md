## Why

Day-to-day contributor loop on this repo has four repetitive steps: create a branch, run precommit checks, commit+push, and surface a PR compare URL. Today that's a 9-command sequence (`git checkout master && git pull upstream master && git checkout -b <branch> && <edits> && go build && python3 -m py_compile <changed> && git add -A && git commit -m ... && git push -u origin HEAD && echo "<compare URL>"`) — every omission is a footgun (forgetting to pull, committing on master, missing the build check, pushing without tracking, forgetting the compare URL). This change replaces the 9 commands with three idempotent scripts that codify the contract.

Same pattern as `scripts/stack_healthcheck.sh` from the previous change: strict bash, colored table, env-overridable, safe to run twice.

## What Changes

- **Add** `scripts/git_ship.sh 'msg'` — commit-push-link-PR. Auto-detects current branch; if on `master`, derives a branch name from the message (conventional-commit prefix + slug) and creates it. Runs `go build ./...` (if any `*.go` is in the staging diff) and `python3 -m py_compile` on every changed `*.py` (if any). Commits with the message, sets upstream to `origin`, pushes, and prints the exact compare URL `https://github.com/swami086/Zovark_swami/compare/master...7inaydas-cmyk:Zovark_swami:<branch>`. Idempotent: a second run with no changes exits 0 after reprinting the URL.
- **Add** `scripts/git_sync.sh` — rebase the current feature branch on top of `upstream/master`. `git fetch upstream`, `git rebase upstream/master`, then `git push --force-with-lease origin HEAD` only if the rebase succeeded. On conflict, prints the conflict count (from `git diff --name-only --diff-filter=U`), leaves the working tree in its rebase-in-progress state, and exits non-zero with a one-line resume hint (`git rebase --continue` after fixing). Refuses to run when `HEAD` is `master` — you don't rebase master on master.
- **Add** `scripts/git_fresh.sh <branch-name>` — start a clean feature branch. Stashes any dirty working tree with a named stash (`git_fresh autostash <timestamp>`), checks out `master`, runs `git pull --ff-only upstream master`, creates `<branch-name>` from that fresh master, and prints a reminder if a stash was saved. Refuses to run if `<branch-name>` already exists locally.
- Shared conventions across all three:
  - `#!/usr/bin/env bash` + `set -euo pipefail` + `MSYS_NO_PATHCONV=1`.
  - TTY-aware colored output (green pass / red fail / yellow warn / gray info). `--no-color` flag available on every script.
  - Verbose by default — each step is announced before it runs. `--quiet` suppresses the step-by-step narration but keeps errors.
  - Exit 0 on success, 1 on hard failure, 2 on a "no-op" degraded case (e.g., ship with nothing to commit → URL is still printed, exit 2).
  - Remote names are overridable via env vars: `ZOVARK_UPSTREAM_REMOTE` (default `upstream`), `ZOVARK_ORIGIN_REMOTE` (default `origin`), `ZOVARK_BASE_BRANCH` (default `master`). PR compare URL is derived from remote URLs with an env override (`ZOVARK_PR_COMPARE_URL_TEMPLATE`), falling back to the hardcoded swami086/7inaydas-cmyk template.
  - **Never** runs `git push --force` (always `--force-with-lease`). **Never** pushes to a branch called `master` on `upstream`. **Never** uses `--no-verify`. **Never** amends already-pushed commits.
  - Executable bit set (mode 0755).
- **Documentation**: brief "Contributor quickstart" block in `CLAUDE.md` pointing at the three scripts, replacing the current 9-command sequence.

## Capabilities

### New Capabilities

- `git-workflow-scripts`: Three operator-facing bash scripts that codify the commit/push/PR-link, upstream-sync, and clean-branch-create loops. Strict idempotency, `--force-with-lease` safety, and a machine-readable exit code contract (0 / 1 / 2).

### Modified Capabilities

- None.

## Impact

- **Affected code**: three new files under `scripts/`; minor `CLAUDE.md` update. No runtime code, no compose files, no migrations, no dependencies.
- **New tool dependencies**: `git` (already required), `curl` is NOT needed, `jq` optional (for future `gh pr create` integration — not in scope here).
- **Runbook**: replaces the 9-command copy-paste flow from `CLAUDE.md` / internal README with three commands.
- **CI**: none — these are operator-side scripts, CI doesn't call them.
- **Risk**: low. All three scripts are git-level only; no state outside the local repo + the fork's branches (which the user owns). The refuse-to-run-on-master guard on `git_sync.sh` and the `git push` → `origin HEAD` restriction on `git_ship.sh` prevent the two ways this class of tool usually injures its users. `--force-with-lease` (not `--force`) on `git_sync.sh` prevents overwriting somebody else's rebase on a shared branch.
