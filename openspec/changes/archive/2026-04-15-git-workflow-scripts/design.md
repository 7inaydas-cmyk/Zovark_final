## Context

The fork-and-upstream workflow on this repo has a specific shape that's easy to get wrong by hand:

- `origin` is the contributor's fork (`7inaydas-cmyk/Zovark_swami`).
- `upstream` is the source of truth (`swami086/Zovark_swami`).
- `master` is the mainline; PRs go `origin:<feature-branch>` → `upstream:master`.
- GitHub's compare URL is a specific non-obvious format: `https://github.com/<upstream-org>/<repo>/compare/<base>...<fork-owner>:<repo>:<head>`.

Today contributors stitch this together manually. Common footguns we've already seen on the current branch:
- Committing on `master` by accident (then `git push origin master` — which happens to not reach upstream, but still clutters the fork).
- Forgetting `git pull upstream master` before branching — rebase conflict two days later.
- Pushing without `-u`, so `git push` on the next iteration still needs the long-form.
- Running the precommit checks out of habit but on the wrong scope — `go build ./...` takes 90 seconds when only `scripts/smoke_test_100.sh` changed.
- Forgetting to paste the compare URL into the PR-tracking channel.
- Using `git push --force` instead of `--force-with-lease` after a rebase.

We want three scripts that each do one thing well, coordinate via shared conventions, and are safe to run twice — mirroring the guardrails the audit-round-2-fixes + stack-healthcheck-script changes already established for this repo's shell layer.

Constraints:
- **Bash only**, no Python, no `gh` CLI required. The `gh` CLI is nice to have but not on every contributor machine.
- **Windows / Git-Bash** support — same `MSYS_NO_PATHCONV=1` posture as the other scripts.
- **No network round trips inside the "prints URL" path** — compare URL is constructed from `git remote get-url` + local branch name; we never hit the GitHub API just to display a URL.
- **Idempotent** — a second run should be a clean no-op (or a clear degraded exit), not a destructive redo.

Stakeholders: every developer on the repo, the CI integration (indirectly — it'll see the same commit shape), and code review (which gets cleaner PR titles from the conventional-commit enforcement).

## Goals / Non-Goals

**Goals:**
- **Three scripts, one contract**: `git_ship.sh`, `git_sync.sh`, `git_fresh.sh` share exit codes, color handling, env var overrides, and `--quiet`/`--no-color` flags.
- **Precommit validation scoped to changed files**: don't build the whole Go tree if only docs changed; don't py_compile the whole worker tree if only one file changed.
- **Never destroy work**: `--force-with-lease`, never amend published commits, never push to `master` on `upstream`, always `--ff-only` on pull.
- **Branch-name derivation from conventional commit messages**: `"fix: dashboard SSE ticket"` → `fix/dashboard-sse-ticket`.
- **Exit codes a human and a CI can both parse**: 0 pass, 1 fail, 2 "no-op degraded" (e.g. ship ran but nothing was committed because the working tree is clean).
- **Print the PR compare URL in the exact format** the user specified, derived from env vars with a safe default.
- **Idempotency**: run `git_ship.sh` twice in a row with the same message — second run prints the URL and exits 2 (no new commit) without error.

**Non-Goals:**
- **Not a PR creator.** We print the URL; we don't call the GitHub API. A follow-up could call `gh pr create` when it's available, but that's not in this change.
- **Not an interactive rebase tool.** `git_sync.sh` does a plain `git rebase upstream/master`. Complex interactive rebases stay manual.
- **Not a commit message linter.** We check the message is non-empty and has a valid conventional-commit-style prefix for branch derivation, but we don't reject long bodies, wrap at 72 chars, or enforce a specific scope list.
- **Not a hooks installer.** Pre-push / pre-commit hooks remain the developer's concern; these scripts don't modify `.git/hooks/`.
- **Not a git tutorial / confirmation-happy wrapper.** No "are you sure?" prompts. Fail fast with a clear message and leave the working tree intact.
- **Not multi-remote aware.** Exactly one `upstream` + one `origin` is assumed. Env vars let the names be overridden, but the model is fixed at two remotes.

## Decisions

### D1 — Three independent scripts, not a single `zv-git` dispatcher
Each script solves a single question ("is this branch ready to ship?" / "is this branch up to date with upstream?" / "give me a fresh branch"). Three small files are easier to review, easier to grep, and easier to paste into muscle memory. A single `zv-git ship|sync|fresh` wrapper is tempting but the dispatch layer adds surface area we don't need. We DO share a single helper file, `scripts/lib/git_common.sh`, sourced by all three for color + exit-code + remote-detection helpers, so we don't repeat the TTY detection block three times.

**Alternative considered**: a single dispatcher `zv-git` with subcommands. Rejected — three files is clearer in `ls scripts/` and the `lib/git_common.sh` file already gets us DRY without the dispatch layer.

### D2 — Branch name derivation: `<type>/<slug>`
When `git_ship.sh` runs on `master`, it needs a branch name. We parse the commit message with a fixed regex: `^(feat|fix|chore|docs|refactor|test|perf|ci|build|style|revert)(\([^)]+\))?:\s*(.+)$`. The type becomes the path prefix; the subject becomes a lowercased, hyphenated slug truncated to 48 chars. So `"fix: dashboard SSE ticket leak"` → `fix/dashboard-sse-ticket-leak`. A message that doesn't match the conventional-commit regex produces an error with a one-line hint: "git_ship: message must start with one of: feat/fix/chore/docs/refactor/test/perf/ci/build/style/revert". We refuse rather than guess, because guessing produces `chore/` branches forever.

**Alternative considered**: accept any message, derive branch name from `git hash-object` or timestamp. Rejected — conventional commits are already the project norm (per existing commit history), and enforcing them at the branch-creation moment is cheap.

### D3 — Scoped precommit: only changed files
`git_ship.sh` runs precommit checks only on files whose status is `A`/`M` in `git status --porcelain`. Two targeted commands:
- If any `*.go` file has changed under `api/`, run `(cd api && go build ./...)`. This builds the whole Go tree because Go packages can't be built in isolation — but only runs at all if Go actually changed.
- If any `*.py` file has changed anywhere, run `python3 -m py_compile <each file>`. This is per-file so the cost is linear in the diff size, not in the repo size.
- If neither `*.go` nor `*.py` changed, skip precommit entirely with an informational line.
The user overrides are `ZOVARK_SKIP_PRECOMMIT=1` (bypass entirely) and `ZOVARK_PRECOMMIT_FULL=1` (run the full `go build ./...` + `python3 -m py_compile worker/**/*.py` regardless of diff).

**Alternative considered**: run `gofmt` / `ruff` / `mypy` / full tests. Rejected — this is a precommit sanity check, not a CI replacement. The first batch of the prior audit established that build + parse is the right bar here.

### D4 — Exit code 2 is the "clean no-op / degraded" signal
Mirroring `stack_healthcheck.sh`:
- `0` = the script did what it was asked and left the repo in the target state.
- `1` = something failed (build error, push rejected, rebase conflict, `git_fresh` refused because branch exists).
- `2` = the script's target state was already achieved (nothing to commit → no new commit but URL still printed; nothing to rebase → no force-push but exit 2; `git_fresh` with a stash that wasn't needed).

`2` lets a CI or wrapper distinguish "you ran this twice" from "you ran it and it broke".

### D5 — `--force-with-lease` always, `--force` never
`git_sync.sh`'s push after rebase uses `git push --force-with-lease=<branch>:<sha> origin HEAD`. If someone else pushed to the same branch while we were rebasing, `--force-with-lease` refuses. This is the single most important safety property in this whole change. We never expose a `--force` flag. If a user really needs `--force` they can type it themselves.

### D6 — Derive the compare URL from git remotes, with env override
The URL template is built from:
```
ZOVARK_PR_COMPARE_URL_TEMPLATE="https://github.com/<upstream-org>/<upstream-repo>/compare/<base>...<origin-org>:<origin-repo>:<head>"
```
where `<upstream-org>/<upstream-repo>` and `<origin-org>/<origin-repo>` are parsed from `git remote get-url upstream` / `git remote get-url origin`, `<base>` is `$ZOVARK_BASE_BRANCH` (default `master`), and `<head>` is the current branch. If the remote parsing fails (e.g. SSH URL with a non-standard format), we fall back to the hardcoded string `https://github.com/swami086/Zovark_swami/compare/master...7inaydas-cmyk:Zovark_swami:<branch>` so the tool doesn't fail on an edge case — and we log a warning about the fallback.

**Alternative considered**: require `gh` and use `gh pr view --web`. Rejected — `gh` isn't universally installed.

### D7 — `git_fresh.sh` uses a named stash, never a throwaway
When the working tree is dirty, we `git stash push -m "git_fresh autostash $(date +%Y%m%d-%H%M%S)" -u`. The `-m` gives the stash a human-readable name so `git stash list` tells the operator exactly which stash came from this tool. `-u` includes untracked files so we don't lose them. We never `git stash drop` afterward — the user must do that explicitly. A final post-checkout message tells them the stash name and how to pop it.

**Alternative considered**: `git stash` without `-u`. Rejected — untracked files (especially new test fixtures) are a common trigger for the "I lost my work" complaint.

### D8 — `git_sync.sh` refuses to run on `master`
If `HEAD` is pointing at `$ZOVARK_BASE_BRANCH` (default `master`), `git_sync.sh` exits 1 with "git_sync: refusing to rebase master on itself. Create a feature branch first (scripts/git_fresh.sh <name>)." This is the second most important safety property after `--force-with-lease`: rebasing master against master is either a no-op or a disaster depending on local commits, and the operator never wants the disaster case.

### D9 — Shared `scripts/lib/git_common.sh`
Common helpers:
- `_emit_info`, `_emit_ok`, `_emit_warn`, `_emit_fail` — colored output with TTY detection + `--no-color` respect.
- `_require_clean_tree` (used by `git_sync.sh`, NOT by `git_ship.sh`).
- `_detect_default_branch` — reads `$ZOVARK_BASE_BRANCH` or falls back to `master`.
- `_parse_github_remote <remote-name>` — prints `<org>/<repo>` or fails.
- `_compare_url <base> <head>` — constructs the full compare URL via D6.
- `_current_branch`, `_has_uncommitted_changes`, `_changed_files`.
All three scripts source this file at the top via `source "$(dirname "$0")/lib/git_common.sh"`. Sourcing is gated behind a presence check that prints a clear error if the file is missing (e.g., if someone copies one script without the lib).

### D10 — Idempotency contract
Each script's idempotency is explicit:
- `git_ship.sh` twice in a row on a clean tree: first run commits + pushes + prints URL (exit 0); second run detects clean tree, prints URL, exits 2 with `degraded: nothing to ship`.
- `git_sync.sh` twice in a row: first run rebases + force-pushes (exit 0); second run finds `origin/HEAD` already at `upstream/master` + current branch, no rebase needed, prints `already up to date`, exits 2.
- `git_fresh.sh <branch>` twice with the same `<branch>`: first run creates + checks out; second run detects the branch already exists locally, exits 1 (this is a failure, not a degraded — the user explicitly asked for a fresh branch and that operation cannot be re-run).

## Risks / Trade-offs

- **[Risk] Operator runs `git_ship.sh` on a feature branch with uncommitted stage changes and a dirty working tree.** → Mitigation: `git_ship.sh` runs `git add -A` only if `--stage-all` is passed; by default it commits whatever is already staged. This matches `git commit -m`'s own semantics and avoids accidentally staging `.env.local` or debug prints. The message "nothing staged to commit — use --stage-all to auto-stage" is printed when the index is empty.
- **[Risk] Branch-name collision when `git_ship.sh` runs on master for a message whose slug already exists as a remote branch.** → Mitigation: after deriving the branch name, we check `git ls-remote --heads origin "$branch"` and append a numeric suffix (`-2`, `-3`, …) if it already exists. Informational line printed.
- **[Risk] `go build ./...` inside `api/` takes 2+ minutes on a cold cache.** → Mitigation: Go's own build cache warms after the first run; the `ZOVARK_SKIP_PRECOMMIT=1` escape hatch exists for hotfix scenarios. The scripts print a `precommit: running go build` line so the user knows where the time is going.
- **[Risk] `python3 -m py_compile` is a weak check — imports aren't resolved.** → Accepted. A stronger check (ruff / mypy / pytest) would drag in optional deps and change the "works on every dev laptop" guarantee. Audit 5.19 already established `py_compile` as the bar for this repo's precommit tier.
- **[Risk] `git rebase` in `git_sync.sh` leaves the working tree in an awkward state on conflict.** → Mitigation: the script tells the user exactly what to do next (`git rebase --continue` after fixing) and prints the conflict count from `git diff --name-only --diff-filter=U | wc -l`. We do NOT call `git rebase --abort` automatically; the user's in-progress merge decisions are their own.
- **[Risk] Force-with-lease still fails on a fast-moving shared branch.** → Accepted. If someone else is on the same branch concurrently, a push failure is the correct outcome. The script exits 1 with the git error text.
- **[Trade-off] No `gh pr create` integration.** Accepted for this change. The printed URL is one click to create a PR in the browser, and the `gh` CLI isn't universally available.
- **[Trade-off] Scripts are linear shell — no parallelism on precommit checks.** Accepted. `go build` and `py_compile` combined finish in well under a minute on a warm cache; parallelism would complicate error reporting for minimal savings.

## Migration Plan

1. **Implementation**: land the three scripts plus `scripts/lib/git_common.sh` in one PR. Update `CLAUDE.md` "Contributor quickstart" section with a 3-line pointer. No runtime code changes.
2. **Adoption**: scripts replace the hand-stitched flow in the operator runbook. Existing branches work unchanged — `git_sync.sh` and `git_ship.sh` operate on whatever branch is currently checked out.
3. **Rollback**: `git revert` the PR. Zero runtime impact.

## Open Questions

- **Q1**: Should `git_ship.sh` optionally create a PR via `gh pr create` when `gh` is available? → **Recommendation**: defer to a follow-up change. Keep this PR limited to bash-only helpers.
- **Q2**: Should the branch-name-derivation regex accept custom conventional-commit types beyond the standard 11? → **Recommendation**: no. The fixed set prevents drift. If someone needs `wip/…` they can type the branch name themselves via `git_fresh.sh wip/foo` and then `git_ship.sh` will pick it up.
- **Q3**: Should `git_sync.sh` support a `--merge` mode for contributors who prefer merge commits? → **Recommendation**: no. The project convention is rebase-based PRs; merge-from-upstream commits add noise.
- **Q4**: Should we add a `git_unship.sh` to undo the last push? → **Recommendation**: no. Undoing a published commit is either `git revert` (safe, obvious) or a force-push (dangerous, should stay manual).
- **Q5**: Should the scripts live in `scripts/` or `scripts/git/`? → **Recommendation**: `scripts/` alongside `stack_healthcheck.sh`, `smoke_test_100.sh`, `backup-db.sh`. The `lib/git_common.sh` helper lives at `scripts/lib/git_common.sh`.
