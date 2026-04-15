## Why

Zovark is moving to a new private GitHub repo, `Zovark_final`, as a single-owner project. Every uncommitted edit from the current session — including the completed `stabilize-runtime-hygiene` work and any staged audit fixes — must land in the git history before the migration, and every fully-completed OpenSpec change must be archived so the new repo starts with a clean, honest `openspec/changes/` directory. The old remote (`7inaydas-cmyk/Zovark_swami`) is kept under a new name `old-origin` so history is preserved and the migration is reversible.

This is a purely operational, no-code-change migration. Nothing in `worker/`, `api/`, `dashboard/`, `helm/`, `k8s/`, `docker-compose*.yml`, or any other runtime artifact is touched beyond what is already committed or in the session working tree.

## What Changes

### 1. Pre-migration commit
- **Commit** every uncommitted file in the working tree as a single coherent commit on the current branch (`audit/execution-fixes`). This captures:
  - Every file edited by `stabilize-runtime-hygiene` (compose, helm, k8s, settings.py, worker/stages/*, finetuning/*, .env.example, CI workflows, HANDOVER.md, activities/__init__.py, investigation_workflow.py, register.py, _legacy_activities.py)
  - Every pre-session dirty file that was already in working-tree state at session start (112 files per initial git status)
  - The four OpenSpec artifacts for `stabilize-runtime-hygiene` (proposal, design, tasks, spec delta)
- **Staging rule**: use explicit `git add <paths>` batched by logical group — NEVER `git add -A` or `git add .` (see `design.md` Decision 4 and the session rule that `git add -A` can sweep up `.env` or other risky files).
- **Commit message**: conventional-commits-style body referencing both `stabilize-runtime-hygiene` and the pre-session audit fixes. One commit, not many — the migration is what this session ships.

### 2. Task-list truth-up for `stabilize-runtime-hygiene`
- Before archiving, **edit** `openspec/changes/stabilize-runtime-hygiene/tasks.md` to tick every checkbox that was actually completed in the apply session (§1–§7 per the session transcript). Sections §6.6/§6.7 remain unticked with a note explaining operator-deferred verification (requires `zovark-inference`).
- This gives future readers an accurate snapshot of what landed vs what was deferred.

### 3. Archive completed OpenSpec changes
- **Move** the following 6 fully-completed changes from `openspec/changes/<name>/` into `openspec/changes/archive/<YYYY-MM-DD>-<name>/` (following the existing archive convention from `2026-04-13-fix-store-stage-schema-blocker`):
  - `database-seed-system` (45/45)
  - `e2e-pipeline-probe` (70/70)
  - `fix-e2e-ingest-stall` (29/29)
  - `git-workflow-scripts` (55/55)
  - `stack-healthcheck-script` (36/36)
  - `telemetry-audit-fix` (63/63)
- **Also archive** `stabilize-runtime-hygiene` once its tasks.md is truthed-up per step 2.
- Changes at 70–97% completion (`apply-migrations-054-071-with-ledger` 61/63, `fix-pgx-pgbouncer-prepared-stmt` 33/36, `fix-store-savepoint-transaction-safety` 43/48, `fix-store-stage-full-schema-gap` 26/37) are **NOT archived by default** — they are active until their last tasks tick. See founder decision F4 in `tasks.md §0`.
- Changes with 0% or <60% completion (`audit-entity-graph-subsystem`, `audit-falsely-backfilled-init-sql-migrations`, `audit-round-2-fixes`, `db-reliability-audit`) are explicitly **left active** in `openspec/changes/`.
- **This change itself** (`migrate-to-zovak-final-repo`) is archived as its own final task (see `tasks.md §7`), in a second commit after the push, so the archive move itself makes it onto the new repo.

### 4. Create new private GitHub repo
- **Create** `https://github.com/7inaydas-cmyk/Zovark_final` as a **private** repo via `gh repo create 7inaydas-cmyk/Zovark_final --private --description "Zovark — autonomous AI SOC investigation platform (private)"`. No `--push`, no `--source`, no `--confirm` — pure empty-repo creation so we control the push sequence ourselves.
- Verify private visibility via `gh repo view 7inaydas-cmyk/Zovark_final --json visibility` before touching remotes.

### 5. Rename `origin` → `old-origin`, add new `origin`
- `git remote rename origin old-origin` (non-destructive, preserves the URL).
- `git remote add origin https://github.com/7inaydas-cmyk/Zovark_final.git`.
- **Do NOT touch `upstream`** — it points at the fork source (`swami086/Zovark_swami`) and is orthogonal to this migration. Leaving it in place preserves the ability to pull upstream changes if ever needed, and the migration's scope is only `origin`.
- Final `git remote -v` state:
  ```
  old-origin  https://github.com/7inaydas-cmyk/Zovark_swami.git  (fetch + push)
  origin      https://github.com/7inaydas-cmyk/Zovark_final.git   (fetch + push)
  upstream    https://github.com/swami086/Zovark_swami.git       (fetch + push)
  ```

### 6. Push full history to new origin
- `git push origin --all` — pushes every local branch including `audit/execution-fixes`, `master`, and any others.
- `git push origin --tags` — pushes every tag (release markers, etc).
- **NOT** `--mirror` and **NOT** `--force`: see design.md Decision 5. Mirror pushes copy remote refs too, which can clobber other branches on the new repo; since the new repo starts empty, mirror is unnecessary and riskier. Force-push is never needed against an empty remote and is explicitly excluded.
- Set upstream tracking for the current branch: `git push -u origin audit/execution-fixes` (or the branch resolved at execute time).

### 7. Second commit: archive `migrate-to-zovak-final-repo` itself
- After the push succeeds and all verification gates pass, move `openspec/changes/migrate-to-zovak-final-repo/` into `openspec/changes/archive/<YYYY-MM-DD>-migrate-to-zovak-final-repo/`.
- Commit the archive move and push it to the new `origin`. This is the final commit of the migration session and proves the new repo is alive and writable.

### 8. Verification gates (MUST all pass before §7)
1. `git status --porcelain` returns empty (working tree clean).
2. `git remote -v` shows exactly three remotes: `origin` → `Zovark_final`, `old-origin` → `Zovark_swami`, `upstream` → fork source.
3. `gh repo view 7inaydas-cmyk/Zovark_final --json visibility -q .visibility` returns `"PRIVATE"`.
4. `gh repo view 7inaydas-cmyk/Zovark_final --json defaultBranchRef` returns the expected branch.
5. `git log origin/<current-branch>..HEAD` is empty (local tip matches new remote tip).
6. `ls openspec/changes/` contains only active (unfinished) changes + this migration change itself + the `archive/` directory. The 6 completed changes listed in §3 are no longer in `openspec/changes/` except under `archive/`.
7. `openspec/changes/archive/<YYYY-MM-DD>-<name>/` exists for each archived change, with `proposal.md`, `design.md`, `tasks.md` intact.
8. `git log --oneline -10` shows the pre-migration commit at HEAD (and the second archive commit after §7).

## Capabilities

### New Capabilities

- `repo-migration`: defines the post-migration repository invariants — old remote preserved under `old-origin`, new `origin` points at a private `Zovark_final`, full git history and tags preserved, completed OpenSpec changes archived, working tree clean. Operational / one-shot but worth capturing as a minimal spec so the post-migration state is auditable.

### Modified Capabilities

- None. No runtime capability is modified by this change.

## Impact

- **Affected paths**:
  - Git working tree: single commit capturing every currently-dirty file
  - `openspec/changes/stabilize-runtime-hygiene/tasks.md` — checkbox truth-up
  - `openspec/changes/{database-seed-system,e2e-pipeline-probe,fix-e2e-ingest-stall,git-workflow-scripts,stack-healthcheck-script,telemetry-audit-fix,stabilize-runtime-hygiene}/` → moved into `openspec/changes/archive/<YYYY-MM-DD>-<name>/`
  - `openspec/changes/migrate-to-zovak-final-repo/` → moved into `openspec/changes/archive/<YYYY-MM-DD>-migrate-to-zovak-final-repo/` as final task
  - Git remote config: `.git/config` — origin renamed, new origin added
  - GitHub: new private repo `7inaydas-cmyk/Zovark_final` created
- **NOT touched**:
  - No code edits in `worker/`, `api/`, `dashboard/`, `helm/`, `k8s/`, `docker-compose*.yml`, `.env.example` beyond what this session already staged
  - No migration files (`migrations/`)
  - No `upstream` remote
  - No deletion of the old remote
  - No force-push, no rewrite of history, no shallow clone
- **Operator impact**: the old remote URL continues to work via `old-origin`. Anyone with the repo cloned locally can update with:
  ```
  git remote rename origin old-origin
  git remote add origin https://github.com/7inaydas-cmyk/Zovark_final.git
  git fetch origin
  ```
- **Rollback**: the old remote still exists on GitHub with all history intact — `git remote rm origin && git remote rename old-origin origin` reverts the local config. The new private repo can be deleted via `gh repo delete 7inaydas-cmyk/Zovark_final --yes` if rollback is required. **Nothing is destroyed on the old remote** — this is a non-destructive migration.
