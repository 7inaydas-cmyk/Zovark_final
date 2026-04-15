## ADDED Requirements

### Requirement: Three executable scripts
The repository SHALL ship three executable scripts — `scripts/git_ship.sh`, `scripts/git_sync.sh`, `scripts/git_fresh.sh` — plus a shared helper file `scripts/lib/git_common.sh`. Each of the three top-level scripts SHALL be committed with mode 0755.

#### Scenario: All three scripts exist and are executable
- **WHEN** a contributor clones the repo and runs `test -x scripts/git_ship.sh && test -x scripts/git_sync.sh && test -x scripts/git_fresh.sh`
- **THEN** the command exits with status 0

#### Scenario: Shared helper exists
- **WHEN** a contributor runs `test -f scripts/lib/git_common.sh`
- **THEN** the command exits with status 0 and the file is sourced by all three top-level scripts

### Requirement: Shared exit code contract
Every script SHALL exit 0 on success, 1 on hard failure, and 2 on a "no-op / degraded" outcome (target state already achieved). Skips and warnings SHALL NOT change the exit code.

#### Scenario: git_ship.sh on a clean tree after a prior successful ship
- **WHEN** the contributor runs `scripts/git_ship.sh 'fix: same message'` twice in a row with no file changes between the two runs
- **THEN** the second run exits with status 2 and prints the same PR compare URL as the first run

#### Scenario: git_sync.sh when already up to date
- **WHEN** the contributor runs `scripts/git_sync.sh` on a feature branch whose tip already equals the common ancestor with `upstream/master`
- **THEN** the script exits with status 2 and prints an "already up to date" message

#### Scenario: git_fresh.sh when the target branch already exists
- **WHEN** the contributor runs `scripts/git_fresh.sh feat/existing-branch` and a local branch named `feat/existing-branch` already exists
- **THEN** the script exits with status 1 and prints a message explaining that `git_fresh.sh` refuses to overwrite an existing branch

### Requirement: git_ship.sh auto-branch derivation
When `scripts/git_ship.sh '<message>'` is invoked on `master`, the script SHALL derive a branch name from the commit message using the pattern `<type>/<slug>`, where `<type>` is the conventional-commit prefix and `<slug>` is a lowercased hyphenated truncation of the subject. The script SHALL create that branch before committing. When invoked off `master`, the script SHALL commit to the current branch without creating a new one.

#### Scenario: Derive branch from a conventional-commit message
- **WHEN** the contributor runs `scripts/git_ship.sh 'fix: dashboard SSE ticket leak'` from `master`
- **THEN** the script creates a branch named `fix/dashboard-sse-ticket-leak` and checks it out before committing

#### Scenario: Reject a non-conventional message
- **WHEN** the contributor runs `scripts/git_ship.sh 'update stuff'` from `master`
- **THEN** the script exits with status 1 and prints a message listing the accepted conventional-commit types (feat, fix, chore, docs, refactor, test, perf, ci, build, style, revert)

#### Scenario: Branch-name collision with existing remote branch
- **WHEN** the script derives a branch name `fix/dashboard` but `origin/fix/dashboard` already exists
- **THEN** the script appends `-2` (or the next available integer) to the branch name and continues

### Requirement: git_ship.sh scoped precommit validation
`git_ship.sh` SHALL run `go build ./...` from the `api/` directory if any staged or tracked-modified `*.go` file is present, and SHALL run `python3 -m py_compile <file>` for every staged or tracked-modified `*.py` file. If neither language has changes, precommit SHALL be skipped with an informational message. The environment variable `ZOVARK_SKIP_PRECOMMIT=1` SHALL bypass all precommit checks.

#### Scenario: Only docs changed
- **WHEN** the contributor runs `git_ship.sh` with only `*.md` files modified
- **THEN** neither `go build` nor `python3 -m py_compile` runs, and the script prints an informational line stating precommit was skipped

#### Scenario: Go file changed
- **WHEN** any `*.go` file is staged or tracked-modified
- **THEN** the script invokes `go build ./...` inside `api/` and aborts the commit with exit 1 if the build fails

#### Scenario: Precommit bypass
- **WHEN** the contributor runs `ZOVARK_SKIP_PRECOMMIT=1 scripts/git_ship.sh 'fix: hot patch'` with dirty Go files
- **THEN** the script does not run `go build` and proceeds directly to the commit

### Requirement: git_ship.sh PR compare URL
After a successful commit and push, `git_ship.sh` SHALL print the PR compare URL in the format `https://github.com/<upstream-org>/<upstream-repo>/compare/<base>...<origin-org>:<origin-repo>:<head>`. The template MAY be overridden via `ZOVARK_PR_COMPARE_URL_TEMPLATE`. If the `upstream` and `origin` remotes cannot be parsed, the script SHALL fall back to the hardcoded default `https://github.com/swami086/Zovark_swami/compare/master...7inaydas-cmyk:Zovark_swami:<branch>` and print a warning.

#### Scenario: URL derivation from standard fork + upstream setup
- **WHEN** `git remote get-url upstream` returns `https://github.com/swami086/Zovark_swami.git` and `git remote get-url origin` returns `https://github.com/7inaydas-cmyk/Zovark_swami.git`, and the script pushes to a branch `fix/dashboard`
- **THEN** the printed URL is `https://github.com/swami086/Zovark_swami/compare/master...7inaydas-cmyk:Zovark_swami:fix/dashboard`

#### Scenario: URL printed even on degraded (no-op) exit
- **WHEN** the script detects there is nothing to ship and exits with status 2
- **THEN** the PR compare URL is still printed before the exit

### Requirement: git_ship.sh push safety
`git_ship.sh` SHALL push with `git push -u origin HEAD` — it SHALL NOT push to any branch on `upstream`, SHALL NOT use `--force`, and SHALL NOT use `--no-verify`.

#### Scenario: Push target is always origin HEAD
- **WHEN** the script runs its push step
- **THEN** the push target is `origin HEAD` and sets upstream tracking to the matching branch on origin

### Requirement: git_sync.sh upstream rebase
`git_sync.sh` SHALL fetch `upstream`, rebase the current branch on top of `<upstream-remote>/<base-branch>`, and then push with `git push --force-with-lease origin HEAD`. It SHALL refuse to run when the current branch is the base branch.

#### Scenario: Refuse to rebase master on master
- **WHEN** the contributor is on `master` and runs `scripts/git_sync.sh`
- **THEN** the script exits with status 1 and prints a message explaining that rebasing the base branch is not supported

#### Scenario: Successful rebase
- **WHEN** a feature branch can be cleanly rebased on top of `upstream/master`
- **THEN** the script performs the rebase, runs `git push --force-with-lease origin HEAD`, and exits 0

#### Scenario: Rebase conflict
- **WHEN** the rebase stops with a merge conflict
- **THEN** the script exits with status 1, prints the conflict count from `git diff --name-only --diff-filter=U`, leaves the repository in its rebase-in-progress state, and prints a one-line hint instructing the operator to run `git rebase --continue` after resolving the conflicts

### Requirement: git_sync.sh force safety
`git_sync.sh` SHALL use `git push --force-with-lease` and SHALL NEVER use `git push --force`.

#### Scenario: Force-with-lease protects against concurrent push
- **WHEN** somebody else has pushed to the same branch on origin since the local rebase started
- **THEN** the `git push --force-with-lease` call fails and the script exits with status 1 without overwriting the remote branch

### Requirement: git_fresh.sh clean branch creation
`git_fresh.sh <branch>` SHALL save any uncommitted changes to a named stash, check out the base branch, fast-forward from `upstream`, and create and check out the requested branch. It SHALL refuse to create a branch whose name already exists locally.

#### Scenario: Clean working tree
- **WHEN** the contributor runs `scripts/git_fresh.sh feat/new-thing` with a clean working tree
- **THEN** the script checks out `master`, runs `git pull --ff-only upstream master`, creates `feat/new-thing`, checks it out, and exits 0

#### Scenario: Dirty working tree
- **WHEN** the contributor runs `scripts/git_fresh.sh feat/new-thing` with unstaged or untracked changes
- **THEN** the script saves the dirty state via `git stash push -u -m "git_fresh autostash <timestamp>"`, proceeds with the fresh checkout, and prints a reminder line telling the operator the stash name and how to restore it

#### Scenario: Non-ff upstream
- **WHEN** the local `master` cannot fast-forward to `upstream/master` (e.g. local commits on master)
- **THEN** the script exits with status 1, prints the `git pull --ff-only` error verbatim, and does not create the new branch

### Requirement: Shared CLI flags and color behavior
Every script SHALL accept the flags `--no-color`, `--quiet`, and `--help`. Colors SHALL be emitted only when stdout is a TTY and `--no-color` is not set. `--quiet` SHALL suppress step-by-step narration but SHALL NOT suppress error output.

#### Scenario: Colors suppressed with --no-color
- **WHEN** any of the scripts is run with `--no-color`
- **THEN** the output contains no ANSI escape sequences regardless of TTY detection

#### Scenario: --help exits 0
- **WHEN** any of the scripts is run with `--help`
- **THEN** it prints usage text naming every supported flag and env var, and exits with status 0

### Requirement: Environment variable overrides
The scripts SHALL honor the following environment variables: `ZOVARK_UPSTREAM_REMOTE` (default `upstream`), `ZOVARK_ORIGIN_REMOTE` (default `origin`), `ZOVARK_BASE_BRANCH` (default `master`), `ZOVARK_PR_COMPARE_URL_TEMPLATE`, `ZOVARK_SKIP_PRECOMMIT`, `ZOVARK_PRECOMMIT_FULL`.

#### Scenario: Alternate remote names
- **WHEN** `ZOVARK_UPSTREAM_REMOTE=swami086 ZOVARK_ORIGIN_REMOTE=fork scripts/git_sync.sh` is run
- **THEN** the script fetches from the remote named `swami086` and pushes to the remote named `fork`

### Requirement: Read-only safety on error paths
If a script fails partway through, it SHALL leave the repository in a state the operator can inspect. It SHALL NOT attempt to `git reset --hard`, `git rebase --abort`, or `git stash drop` as part of its own error handling.

#### Scenario: Build failure does not touch git state
- **WHEN** `git_ship.sh` runs `go build` and the build fails
- **THEN** the script exits with status 1 without creating a commit, without creating or deleting any stash, and without changing branches

#### Scenario: Rebase failure leaves the rebase in progress
- **WHEN** `git_sync.sh` encounters a rebase conflict
- **THEN** the repository remains in the rebase-in-progress state (as reported by `git status`) and the script does not call `git rebase --abort`
