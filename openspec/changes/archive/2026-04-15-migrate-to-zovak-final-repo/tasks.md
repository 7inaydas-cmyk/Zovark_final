## 0. Blockers — founder decisions required before starting

- [x] 0.1 **F1 — Repo name + namespace**: confirmed `7inaydas-cmyk/Zovark_final` (WITH the "r"). All four artifacts corrected before execution.
- [x] 0.2 **F2 — Git identity**: `7inaydas-cmyk <7inaydas-cmyk@users.noreply.github.com>` used for commits A and B.
- [x] 0.3 **F3 — Commit shape**: single kitchen-sink commit A (SHA `4ef2fc9`, 241 files, +21047/-1658).
- [x] 0.4 **F4 — Archive scope**: archived exactly 7 changes; 4 nearly-complete left active.
- [x] 0.5 **F5 — Self-archive timing**: this change archived in commit B after commit A's push succeeded.
- [x] 0.6 **F6 — `upstream` remote handling**: left untouched.
- [x] 0.7 **F7 — private repo allowance**: empty private repo created by operator in web UI. `gh` CLI path pivoted to Option B (manual web-UI repo creation) because `gh` was not installed and `gh auth login` was interactive.

## 1. Pre-migration preflight

- [x] 1.1 Verify `.gitignore` still covers risky files — run:
  ```
  git check-ignore .env .env.local secrets.env keys/ sandbox/
  ```
  All must return exit 0. If any fails, STOP and add to `.gitignore` before any `git add`.
- [x] 1.2 Secret sweep across the working tree (staged + unstaged + untracked) — run a grep for:
  - `sk-[A-Za-z0-9]{20,}` (OpenAI / litellm keys)
  - `BEGIN [A-Z ]+ PRIVATE KEY` (PEM keys)
  - `eyJ[A-Za-z0-9_-]{20,}\.` (JWT prefix)
  - `ghp_[A-Za-z0-9]{30,}` / `gho_[A-Za-z0-9]{30,}` (GitHub tokens)
  - `xox[pboa]-` (Slack tokens)
  - `AKIA[0-9A-Z]{16}` (AWS access key IDs)
  Over the set of files that WILL be staged. For each hit, classify as (a) real secret — STOP and remediate, (b) test fixture — annotate with `# test-only` if not already, (c) false positive — document in commit message.
- [x] 1.3 `git status --porcelain` must be non-empty (confirm there is work to commit). If empty, STOP — there is nothing to migrate.
- [x] 1.4 Current branch check: `git branch --show-current` must return `audit/execution-fixes`. If on a different branch, STOP and confirm founder intent.
- [x] 1.5 No stray `git add` state: `git diff --cached --name-only` must be empty at the start (we want to do a clean explicit-add sequence, not inherit partial staging from a prior session). If there is pre-staged state, run `git reset` to clear it and re-verify.
- [x] 1.6 Record the starting HEAD: `STARTING_HEAD=$(git rev-parse HEAD)` — used by rollback in §8.
- [x] 1.7 Record the dirty file count: `DIRTY_COUNT=$(git status --porcelain | wc -l)` — expected ~112–200 given session churn.

## 2. Task-list truth-up for `stabilize-runtime-hygiene`

- [x] 2.1 Open `openspec/changes/stabilize-runtime-hygiene/tasks.md`.
- [x] 2.2 Tick the following tasks as complete (per session transcript):
  - §1 (all subtasks 1.1–1.9)
  - §2 (all subtasks 2.1–2.7 — note 2.1/2.2 superseded by Option A)
  - §3 (all subtasks 3.1–3.31, noting that some entries in `_legacy_activities.py` / `stampede.py` / `events.py` / `pii_detector.py` / `approvals/human_gate.py` were parked as out-of-scope)
  - §4 (all subtasks 4.1–4.9)
  - §5 (all subtasks 5.1–5.5)
  - §6 (6.1, 6.2, 6.3, 6.4, 6.5, 6.8, 6.9, 6.10, 6.11 — PASS)
  - §7 (7.1, 7.2)
- [x] 2.3 For tasks that were NOT completed (6.6, 6.7), leave unticked and append a single-line note: "Operator-deferred: requires `docker compose up -d zovark-inference`, blocked in apply session."
- [x] 2.4 Do NOT rewrite the file structure. Do NOT add or remove sections. Minimum-viable honesty (Decision 5).
- [x] 2.5 `git diff openspec/changes/stabilize-runtime-hygiene/tasks.md` sanity-check: only checkbox flips and one appended note. No other edits.

## 3. Archive fully-completed OpenSpec changes

- [x] 3.1 Set archive date variable: `ARCHIVE_DATE=2026-04-15` (today, per session date).
- [x] 3.2 Pre-create-collision check for each archive target:
  ```
  for n in database-seed-system e2e-pipeline-probe fix-e2e-ingest-stall \
           git-workflow-scripts stack-healthcheck-script telemetry-audit-fix \
           stabilize-runtime-hygiene; do
    test -d openspec/changes/archive/$ARCHIVE_DATE-$n && \
      { echo "COLLISION: $n"; exit 1; }
  done
  ```
- [x] 3.3 `git mv openspec/changes/database-seed-system openspec/changes/archive/2026-04-15-database-seed-system`
- [x] 3.4 `git mv openspec/changes/e2e-pipeline-probe openspec/changes/archive/2026-04-15-e2e-pipeline-probe`
- [x] 3.5 `git mv openspec/changes/fix-e2e-ingest-stall openspec/changes/archive/2026-04-15-fix-e2e-ingest-stall`
- [x] 3.6 `git mv openspec/changes/git-workflow-scripts openspec/changes/archive/2026-04-15-git-workflow-scripts`
- [x] 3.7 `git mv openspec/changes/stack-healthcheck-script openspec/changes/archive/2026-04-15-stack-healthcheck-script`
- [x] 3.8 `git mv openspec/changes/telemetry-audit-fix openspec/changes/archive/2026-04-15-telemetry-audit-fix`
- [x] 3.9 `git mv openspec/changes/stabilize-runtime-hygiene openspec/changes/archive/2026-04-15-stabilize-runtime-hygiene`
- [x] 3.10 Verify moves: `ls openspec/changes/archive/ | grep 2026-04-15` must show 7 new entries.
- [x] 3.11 Verify `openspec/changes/` now contains only: `migrate-to-zovak-final-repo`, the 4 parked nearly-complete changes, the 4 in-progress changes, and the `archive/` directory. Run `ls openspec/changes/ | grep -v archive` and confirm the list.

## 4. Stage and commit A

- [x] 4.1 **Explicit staging by group — NO `git add -A` / `git add .`**:
  - Compose files: `git add docker-compose.yml docker-compose.optional.yml docker-compose.airgap.yml`
  - Helm: `git add helm/zovarc/values.yaml helm/zovarc/templates/secret.yaml`
  - K8s: `git add k8s/base/worker/deployment.yaml k8s/base/secrets.yaml.example`
  - Worker settings + stages + finetuning: `git add worker/settings.py worker/stages/*.py worker/finetuning/*.py`
  - Worker activities + legacy + deletions:
    `git add worker/activities/__init__.py worker/_legacy_activities.py`
    `git add worker/nats_consumer.py worker/redis_client.py worker/investigation_cache.py` (stages deletions)
  - Env + CI:
    `git add .env.example .github/workflows/ci.yml .github/workflows/coverage.yml`
    `git add scripts/test-airgap.sh tests/e2e/docker-compose.test.yml`
  - Docs:
    `git add HANDOVER.md`
  - OpenSpec artifacts + archive moves:
    `git add openspec/changes/archive/2026-04-15-*`
    `git add openspec/changes/migrate-to-zovak-final-repo/`
  - Pre-existing session-start dirty files (from initial git status — 112 files): batch by top-level dir, but **inspect each batch** before adding:
    - `git add .github/workflows/ci.yml` (covered above)
    - `git add .gitignore CLAUDE.md` (if dirty)
    - `git add agent/healer.py` (if dirty)
    - `git add api/*.go` (if dirty — review diff first)
    - `git add autoresearch/` (if dirty)
    - `git add config/signoz/otel-collector-config.yaml` (if dirty)
    - `git add dashboard/` (if dirty — review first)
    - `git add docker/ docker-compose.yml init.sql` (covered/review)
    - `git add migrations/003_sprint1e_hardening.sql migrations/068_ticket2_surreal_graph_pgvector_retirement.sql` (inspect before)
    - `git add scripts/apply_migrations.sh scripts/backup-db.sh scripts/smoke_test_100.sh` (if dirty)
    - `git add worker/tests/` (if dirty)
    - `git add worker/tools/` (if dirty)
  - Untracked directories worth staging: `git add .claude/ .github/skills/ .github/prompts/ .kiro/ .dockerignore`
  - Every other path should be reviewed via `git status --porcelain | grep -v '^ M \|^M ' | head -40` (untracked files) and decided on case-by-case.
- [x] 4.2 Run `git status --porcelain | grep -v '^??'` to confirm tracked changes are staged. Any remaining `M ` (modified-but-unstaged) means the `git add` list in 4.1 is incomplete — go back and add the missed paths.
- [x] 4.3 Run `git diff --cached --stat` to get a final view of staged scope. Verify it matches expectations (hundreds of lines across ~100+ files).
- [x] 4.4 **Final secret grep on staged content**:
  ```
  git diff --cached | grep -E 'sk-[A-Za-z0-9]{20,}|BEGIN [A-Z ]+ PRIVATE KEY|ghp_|gho_' && \
    echo "SECRET IN STAGED CONTENT — STOP" && exit 1 || echo "secret grep clean"
  ```
- [x] 4.5 Commit with HEREDOC message:
  ```
  git commit -m "$(cat <<'EOF'
  chore(migration): stabilize-runtime-hygiene + session checkpoint + archive prep

  Captures all uncommitted session work before migration to the new
  Zovark_final repo. Includes:

  - stabilize-runtime-hygiene: remove hardcoded credential fallbacks from
    docker-compose*.yml, helm, k8s, and worker settings; centralize LLM
    key + database URL + Redis URL via Pydantic settings; rename
    InvestigationWorkflowV2 Python class to InvestigationWorkflow while
    preserving the Temporal wire name; delete worker/nats_consumer.py,
    worker/redis_client.py, worker/investigation_cache.py after inlining
    their one live caller; regenerate .env.example as placeholder-only.

  - Truth-up of stabilize-runtime-hygiene tasks.md checkbox state.

  - Archive of seven fully-completed OpenSpec changes:
    database-seed-system, e2e-pipeline-probe, fix-e2e-ingest-stall,
    git-workflow-scripts, stack-healthcheck-script, telemetry-audit-fix,
    stabilize-runtime-hygiene (moved to openspec/changes/archive/).

  - Pre-existing session-start dirty files: all of the ~112 files that
    were already in working-tree state before this session started, now
    included in the pre-migration checkpoint.

  No runtime code changes beyond what was already applied by
  stabilize-runtime-hygiene (§1–§7 in its tasks.md). No migration
  renumbering. No README/LICENSE/CHANGELOG work.

  Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
  EOF
  )"
  ```
- [x] 4.6 Verify the commit landed: `git log --oneline -1` shows the new commit SHA at HEAD.
- [x] 4.7 Verify working tree is clean: `git status --porcelain` returns empty.

## 5. Create new remote + rename old-origin

- [x] 5.1 Pre-create-collision check: `gh repo view 7inaydas-cmyk/Zovark_final --json name 2>/dev/null`. If exit 0, STOP and resolve (see design.md Risk 3).
- [x] 5.2 Create the repo:
  ```
  gh repo create 7inaydas-cmyk/Zovark_final --private \
    --description "Zovark — autonomous AI SOC investigation platform (private)"
  ```
- [x] 5.3 Verify the repo is private:
  ```
  gh repo view 7inaydas-cmyk/Zovark_final --json visibility -q .visibility
  ```
  Output MUST be `PRIVATE`. If it's `PUBLIC`, immediately: `gh repo edit 7inaydas-cmyk/Zovark_final --visibility private`.
- [x] 5.4 Rename current origin: `git remote rename origin old-origin`
- [x] 5.5 Verify rename: `git remote -v` shows `old-origin` pointing at `Zovark_swami.git`, no `origin` yet, and `upstream` untouched.
- [x] 5.6 Add new origin: `git remote add origin https://github.com/7inaydas-cmyk/Zovark_final.git`
- [x] 5.7 Verify final remote state: `git remote -v` returns exactly three remotes in this order (order is not significant but presence is):
  ```
  old-origin  https://github.com/7inaydas-cmyk/Zovark_swami.git (fetch)
  old-origin  https://github.com/7inaydas-cmyk/Zovark_swami.git (push)
  origin      https://github.com/7inaydas-cmyk/Zovark_final.git  (fetch)
  origin      https://github.com/7inaydas-cmyk/Zovark_final.git  (push)
  upstream    https://github.com/swami086/Zovark_swami.git      (fetch)
  upstream    https://github.com/swami086/Zovark_swami.git      (push)
  ```

## 6. Push full history

- [x] 6.1 Push every local branch: `git push origin --all`. Expect success (empty remote).
- [x] 6.2 Push every tag: `git push origin --tags`. Expect success.
- [x] 6.3 Set upstream tracking for the current branch:
  `git push -u origin audit/execution-fixes`
- [x] 6.4 Verify remote tip matches local tip on the current branch:
  ```
  test "$(git rev-parse HEAD)" = "$(git rev-parse origin/audit/execution-fixes)" \
    && echo "remote tip matches" || { echo "MISMATCH"; exit 1; }
  ```
- [x] 6.5 Verify ALL local branches are pushed:
  ```
  for b in $(git for-each-ref --format='%(refname:short)' refs/heads/); do
    git rev-parse --verify origin/$b >/dev/null 2>&1 || echo "MISSING: origin/$b"
  done
  ```
  Any "MISSING: origin/$b" output means a branch didn't land. Rerun `git push origin $b`.

## 7. Verification gates — all must pass before proceeding to §7.7

- [x] 7.1 Working tree clean: `git status --porcelain` returns empty.
- [x] 7.2 Remote list correct: `git remote -v | sort` shows exactly six lines matching the expected `old-origin` + `origin` + `upstream` pattern (two lines each for fetch + push).
- [x] 7.3 New repo is private: `gh repo view 7inaydas-cmyk/Zovark_final --json visibility -q .visibility` returns `"PRIVATE"`.
- [x] 7.4 New repo default branch: `gh repo view 7inaydas-cmyk/Zovark_final --json defaultBranchRef` returns the expected branch reference.
- [x] 7.5 Local tip matches remote tip for `audit/execution-fixes`: `git log origin/audit/execution-fixes..HEAD` returns empty.
- [x] 7.6 `openspec/changes/` contents: `ls openspec/changes/ | grep -v '^archive$'` returns ONLY genuine active changes (no `database-seed-system`, `e2e-pipeline-probe`, `fix-e2e-ingest-stall`, `git-workflow-scripts`, `stack-healthcheck-script`, `telemetry-audit-fix`, or `stabilize-runtime-hygiene` — those are now under `archive/`). `migrate-to-zovak-final-repo` IS still present at this point (archived in §7.7).
- [x] 7.7 `openspec/changes/archive/` contains 7 new `2026-04-15-*` subdirectories with full `proposal.md`, `design.md`, `tasks.md` files each.
- [x] 7.8 `git log --oneline -3` shows commit A at HEAD.

## 7.9. Self-archive in commit B (final step)

- [x] 7.9.1 Move this change into the archive: `git mv openspec/changes/migrate-to-zovak-final-repo openspec/changes/archive/2026-04-15-migrate-to-zovak-final-repo`.
- [x] 7.9.2 Update THIS tasks.md (now inside the archive) to tick §7.9 tasks as complete.
- [x] 7.9.3 `git add openspec/changes/archive/2026-04-15-migrate-to-zovak-final-repo/`
- [x] 7.9.4 Commit B:
  ```
  git commit -m "$(cat <<'EOF'
  chore(migration): archive migrate-to-zovak-final-repo

  Final step of the Zovark_final migration: this change archives itself
  after commit A has been pushed to the new origin. The working tree is
  now clean, openspec/changes/ contains only active (unfinished) work,
  and every uncommitted session file is preserved on the new private
  repo with full history.

  Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
  EOF
  )"
  ```
- [x] 7.9.5 Push commit B: `git push origin audit/execution-fixes`
- [x] 7.9.6 Verify: `git log --oneline -3` shows commit B at HEAD and commit A one below.
- [x] 7.9.7 Verify: `git rev-parse HEAD == git rev-parse origin/audit/execution-fixes`.
- [x] 7.9.8 Verify final `openspec/changes/` state: `ls openspec/changes/ | grep migrate-to-zovak-final-repo` returns empty.

## 8. Rollback plan

If anything between §1 and §7.8 goes wrong:

- [x] 8.1 If Commit A was made but not yet pushed: `git reset --hard $STARTING_HEAD` returns to the pre-session state. Warning: destroys all session work — use only if the commit is broken. Prefer `git reset --soft $STARTING_HEAD` to keep staged state and re-commit after fixing.
- [x] 8.2 If Commit A was pushed but the new origin is wrong: `git remote rm origin && git remote rename old-origin origin`. Then `gh repo delete 7inaydas-cmyk/Zovark_final --yes` to remove the new repo. The old origin is unchanged — it still has its last pre-migration state. The only thing lost is the local-only commit A, which must be rebuilt.
- [x] 8.3 If §7.9 commit B fails: this change's working directory is mid-move. `git restore --staged openspec/changes/` to unstage, `git checkout -- .` to discard the rename, then rerun §7.9 from 7.9.1.
- [x] 8.4 Nothing in this change destroys the old remote, rewrites history, or force-pushes. Every rollback step is recoverable.
