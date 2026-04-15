## ADDED Requirements

### Requirement: New `origin` is a private GitHub repo at `Zovark_final`
After the migration completes, the git remote named `origin` SHALL point at `https://github.com/7inaydas-cmyk/Zovark_final.git`, and the GitHub repository at that URL SHALL have visibility `PRIVATE`.

#### Scenario: Origin points at the new repo
- **WHEN** an operator runs `git remote get-url origin`
- **THEN** the output is `https://github.com/7inaydas-cmyk/Zovark_final.git`

#### Scenario: New repo is private
- **WHEN** an operator runs `gh repo view 7inaydas-cmyk/Zovark_final --json visibility -q .visibility`
- **THEN** the output is `"PRIVATE"`

### Requirement: Old remote preserved as `old-origin`
The previous `origin` (pointing at `https://github.com/7inaydas-cmyk/Zovark_swami.git`) SHALL be preserved locally under the remote name `old-origin`. Neither the remote reference nor the remote repository itself is deleted.

#### Scenario: old-origin remote exists
- **WHEN** an operator runs `git remote get-url old-origin`
- **THEN** the output is `https://github.com/7inaydas-cmyk/Zovark_swami.git`

#### Scenario: Old GitHub repository still exists
- **WHEN** an operator runs `gh repo view 7inaydas-cmyk/Zovark_swami --json name -q .name`
- **THEN** the output is `"Zovark_swami"` (the old repo is not deleted)

### Requirement: `upstream` remote untouched
The `upstream` remote pointing at the fork source (`https://github.com/swami086/Zovark_swami.git`) SHALL remain configured unchanged. The migration SHALL NOT rename or delete this remote.

#### Scenario: upstream remote is unchanged
- **WHEN** an operator runs `git remote get-url upstream`
- **THEN** the output is `https://github.com/swami086/Zovark_swami.git`

### Requirement: Full history pushed to new origin
Every local branch and tag present at migration time SHALL be pushed to the new `origin`. No history rewriting, squashing, shallow-cloning, or force-pushing SHALL occur during the migration.

#### Scenario: Current branch tip matches remote
- **WHEN** an operator runs `git log origin/audit/execution-fixes..HEAD`
- **THEN** the output is empty (local and remote tips are identical)

#### Scenario: Every local branch has a remote counterpart
- **WHEN** an operator iterates every local branch and runs `git rev-parse --verify origin/<branch>` for each
- **THEN** every invocation succeeds

#### Scenario: No force-push was used
- **WHEN** a reviewer inspects the session transcript or git reflog for the migration commits
- **THEN** neither `git push --force` nor `git push -f` appears in any command executed during the migration

### Requirement: Working tree clean after migration
After the migration's final commit is pushed, the local working tree SHALL be clean — no uncommitted files, no staged changes, no untracked files that represent session work.

#### Scenario: git status is clean
- **WHEN** an operator runs `git status --porcelain`
- **THEN** the output is empty

### Requirement: Completed OpenSpec changes archived
After the migration, the directory `openspec/changes/` SHALL contain only OpenSpec changes that are genuinely active (not fully completed). Every change that was at 100% task completion at migration time SHALL have been moved under `openspec/changes/archive/<YYYY-MM-DD>-<name>/`. The four changes identified as nearly-complete (`apply-migrations-054-071-with-ledger`, `fix-pgx-pgbouncer-prepared-stmt`, `fix-store-savepoint-transaction-safety`, `fix-store-stage-full-schema-gap`) are explicitly allowed to remain under `openspec/changes/` if their last tasks have not been ticked.

#### Scenario: Completed changes are no longer under openspec/changes/
- **WHEN** a reviewer runs `ls openspec/changes/ | grep -E '^(database-seed-system|e2e-pipeline-probe|fix-e2e-ingest-stall|git-workflow-scripts|stack-healthcheck-script|telemetry-audit-fix|stabilize-runtime-hygiene|migrate-to-zovak-final-repo)$'`
- **THEN** the output is empty

#### Scenario: Archive subdirectories exist for each archived change
- **WHEN** a reviewer runs `ls openspec/changes/archive/ | grep 2026-04-15`
- **THEN** the output contains exactly these 8 entries: `2026-04-15-database-seed-system`, `2026-04-15-e2e-pipeline-probe`, `2026-04-15-fix-e2e-ingest-stall`, `2026-04-15-git-workflow-scripts`, `2026-04-15-migrate-to-zovak-final-repo`, `2026-04-15-stabilize-runtime-hygiene`, `2026-04-15-stack-healthcheck-script`, `2026-04-15-telemetry-audit-fix`

#### Scenario: Each archived change retains its full artifacts
- **WHEN** a reviewer inspects any `openspec/changes/archive/2026-04-15-<name>/` directory
- **THEN** the directory contains `proposal.md`, `design.md`, and `tasks.md` files (and `specs/<capability>/spec.md` where applicable)

### Requirement: No runtime code modified by the migration
The migration SHALL NOT modify any file under `worker/`, `api/`, `dashboard/`, `helm/`, `k8s/`, `docker-compose*.yml`, `migrations/`, or `.env.example` beyond what was already in the working tree at migration start. The only files written by the migration itself are (a) OpenSpec task checkbox updates for `stabilize-runtime-hygiene`, (b) `git mv` directory moves under `openspec/changes/archive/`, and (c) this change's own archive move.

#### Scenario: Migration commits contain only truth-up + archive moves
- **WHEN** a reviewer diffs commit B against its parent with `git show --stat <commit-B-sha>`
- **THEN** only paths under `openspec/changes/archive/2026-04-15-migrate-to-zovak-final-repo/` appear in the diff
