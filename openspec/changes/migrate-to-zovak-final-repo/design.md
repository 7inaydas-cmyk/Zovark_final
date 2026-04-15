## Context

Zovark is currently hosted at `https://github.com/7inaydas-cmyk/Zovark_swami.git` (origin) with a fork relationship to `https://github.com/swami086/Zovark_swami.git` (upstream). The project is moving to a new single-owner private repo, `https://github.com/7inaydas-cmyk/Zovark_final.git`. The session at the point of this change has:

- **112 uncommitted working-tree files** (per initial session `git status`)
- **Completed session work**: `stabilize-runtime-hygiene` (all 7 sections applied — see session transcript), plus whatever was already dirty before session start
- **Current branch**: `audit/execution-fixes`
- **Committed Git user**: `7inaydas-cmyk <7inaydas-cmyk@users.noreply.github.com>` (verified)
- **6 fully-completed OpenSpec changes** eligible for archive (100% tasks ticked)
- **1 session-completed change** (`stabilize-runtime-hygiene`) whose tasks.md checkboxes were never ticked during apply (2/82 on disk, but code is done)
- **4 partially-complete changes** (60–97%) — explicit non-scope
- **4 in-progress changes** (0–56%) — explicit non-scope
- **`upstream` remote** pointing at `swami086/Zovark_swami` — explicit non-scope

**Stakes:**
- Any commit that leaks `.env` secrets, LLM keys, or bcrypt hashes is irrecoverable on a public-facing history even if the repo is private.
- Any force-push against the new `Zovark_final` repo could accidentally wipe commits that another session pushed concurrently (single-owner, but belt-and-braces).
- Losing history via `--shallow` or `--single-branch` push would erase ~year of audit, sprint, and fix commits.
- Renaming `origin` wrong — or accidentally deleting it — loses the rollback route to the old repo.

**Non-negotiables (from scope):**
- No code modification beyond what's already dirty in the working tree
- Preserve full git history (no rewrites, no rebases, no squashes)
- Old remote NOT deleted — renamed to `old-origin`
- No new work after migration (this change is the last work of the session)

## Goals / Non-Goals

**Goals:**
- Exactly **two commits** produced by this change:
  1. **Commit A** — pre-migration catch-all: every uncommitted file, plus `stabilize-runtime-hygiene` tasks.md checkbox truth-up, plus the 7 archive moves.
  2. **Commit B** — archive move for `migrate-to-zovak-final-repo` itself, after the push of commit A succeeds.
- After the final push:
  - `git status` is clean.
  - `git remote -v` shows `origin`→Zovark_final, `old-origin`→Zovark_swami, `upstream`→fork.
  - `gh repo view 7inaydas-cmyk/Zovark_final --json visibility` returns `PRIVATE`.
  - `openspec/changes/` contains **only** genuinely-active changes, plus `archive/` (which now contains 7 new subdirectories).
  - Every local branch and tag is pushed to `origin`.
  - `git log --oneline -3` on `audit/execution-fixes` shows commit A and commit B at the top.

**Non-Goals:**
- Migrating the `upstream` remote. Left alone.
- Archiving partially-complete changes (even the 90%+ ones). Founder decision required to pull any of those in.
- Editing `apply-migrations-054-071-with-ledger`, `fix-pgx-pgbouncer-prepared-stmt`, `fix-store-savepoint-transaction-safety`, `fix-store-stage-full-schema-gap` to close their last few tasks. Out of scope.
- Rewriting or squashing git history. Commit A is a single catch-all; it's verbose, not clean, and that's intentional.
- Rotating any credentials on disk. Founder decision F5 from `stabilize-runtime-hygiene` already ruled this out of code scope.
- Deleting the old GitHub repo. Scope says "rename, don't delete".
- Creating GitHub Actions / branch protection / CODEOWNERS / secret scanning on the new repo. Those are follow-up operator tasks; this change only lands the code.
- Updating CI workflows to reference the new repo URL (they don't — they use `github.repository` dynamically).

## Decisions

### Decision 1 — Two commits, not one

**What:** Commit A captures every dirty file + archive moves for the 7 completed changes. Commit B archives this migration change itself after the push succeeds.

**Why:** Archiving a change that hasn't executed yet produces a dishonest archive (tasks.md with zero ticks). Archiving this change as part of commit A means the archive would claim "done" while the push hasn't happened. Two commits lets the archive of migrate-to-zovak-final-repo reflect reality — it's archived AFTER the push that proves the migration worked.

**Alternatives:**
- **One commit, archive this change as part of commit A.** Rejected — dishonest archive state.
- **Never archive this change; leave it active forever.** Rejected — the new repo then starts with a pseudo-active change that's effectively completed. Tech debt.
- **Archive via a PR on the new repo after push.** Rejected — out of single-session scope and requires a new session to approve.

### Decision 2 — Staged adds only, never `git add -A`

**What:** Use explicit `git add <path1> <path2> ...` invocations, grouped by logical area (compose, helm, k8s, worker/settings.py, worker/stages, worker/finetuning, worker/activities, worker/_legacy_activities.py, .env.example, .github/workflows, HANDOVER.md, openspec/changes/stabilize-runtime-hygiene, openspec/changes/migrate-to-zovak-final-repo). Never `git add -A`, never `git add .`.

**Why:** The project has gitignored `.env` on disk with real values, plus potential `sandbox/` artifacts, `overnight_report_*` directories, `__pycache__` caches, and any `.pem`/`.key` files. A blanket add risks staging a secret even though `.gitignore` catches most of them. Explicit paths force the operator to see exactly what's being committed.

Session-start rules explicitly say "Never use `git add -A` or `git add .`" — this decision re-affirms it.

**Alternatives:**
- `git add -A` for speed. Rejected per rules.
- `git add -u` (only tracked modifications, no new files). Rejected — session added new OpenSpec artifacts (both `stabilize-runtime-hygiene/` and this change's own dir) that are untracked and must be added.

### Decision 3 — `git push origin --all --tags`, NOT `--mirror`, NEVER `--force`

**What:** Push every local branch with `git push origin --all`, then every tag with `git push origin --tags`. Do not use `--mirror`. Never `--force`.

**Why:**
- `--all` pushes every local branch to the new origin. Since the new repo is empty, every push is a clean first-write with no conflicts.
- `--tags` pushes every tag (release markers). Full history includes tags.
- `--mirror` pushes refs that are NOT local branches/tags too — remote-tracking refs, notes, etc. That can include cruft or, worse, rewrite the new repo's ref namespace in surprising ways. Since new repo is empty, mirror provides no benefit over `--all --tags`.
- `--force` is unnecessary against an empty remote and is banned by the session rules ("NEVER run force push to main/master, warn the user if they request it"). An explicit rule restating this kept in the tasks list as a guard.

**Alternatives:**
- `git push origin --mirror`. Rejected — risk of pushing stale remote-tracking refs.
- `git push origin HEAD`. Rejected — only pushes current branch, drops other local branches and tags.

### Decision 4 — Create empty repo first, push second

**What:** `gh repo create 7inaydas-cmyk/Zovark_final --private --description "..."` with NO `--source`, NO `--push`, NO `--confirm`. Then manually rename origin, add new origin, push.

**Why:** The "one-shot" `gh repo create --source=. --push --private` form silently does a shallow push of the current branch only; it does not push all branches + tags, and it silently adds a new `origin` remote on top of the existing one (which would either conflict or get renamed arbitrarily). We want full control over:
1. Repo name and description (one command)
2. Remote renaming (one command)
3. New remote addition (one command)
4. `--all` push (one command)
5. `--tags` push (one command)
6. Upstream tracking (one command)

Six explicit commands with verifiable intermediate state is safer than one multi-action command that hides what it did.

**Alternatives:**
- `gh repo create --source=. --push`. Rejected — hides the remote sequence.
- Create via web UI. Rejected — manual, not scriptable, not idempotent.

### Decision 5 — Tasks.md truth-up for `stabilize-runtime-hygiene` as a PATCH, not a rewrite

**What:** Edit the existing tasks.md file in place to change `- [ ]` to `- [x]` for every task that was actually completed in the session. Do NOT rewrite the file or reorder sections. Add a short note under §6.6/§6.7 explaining operator-deferred verification.

**Why:** History must be truthful but also minimal. The alternative is to leave tasks.md at 2/82 (which misrepresents the actual state) or to rewrite it as a polished retrospective (scope creep, and it rewrites history that's fine as-is). In-place tick-mark edits are the minimum-viable honesty.

**Alternatives:**
- Leave tasks.md unchanged. Rejected — the archive would be misleading.
- Rewrite tasks.md as a post-mortem. Rejected — not the job of this change, and the tasks.md format is not a post-mortem format.

### Decision 6 — `upstream` remote is left alone

**What:** `git remote -v` after migration shows `origin`, `old-origin`, AND `upstream`. The fork-source `upstream` is not renamed, not deleted.

**Why:** The user scope says "rename it to old-origin" — referring to origin. Upstream is orthogonal: it points at the canonical fork source (`swami086/Zovark_swami`), not the old publish target. Renaming it would change the semantics of `git fetch upstream` and confuse anyone who still wants to sync from the fork parent.

**Alternatives:**
- Rename `upstream` → `old-upstream`. Rejected — out of scope and semantically wrong.
- Delete `upstream`. Rejected — out of scope and destroys the fork sync path.

### Decision 7 — Founder decision F4: nearly-complete changes are NOT archived by default

**What:** `apply-migrations-054-071-with-ledger` (61/63), `fix-pgx-pgbouncer-prepared-stmt` (33/36), `fix-store-savepoint-transaction-safety` (43/48), and `fix-store-stage-full-schema-gap` (26/37) stay active. They are NOT archived in this change unless the founder explicitly pulls them in.

**Why:** The last 2–5 tasks on each of these could be either:
(a) trivial doc/comment tasks that never ticked because they were forgotten, or
(b) real unfinished work that would be lost if the change is archived prematurely.

Archiving without knowing which category each is in could silently drop unfinished work from the tracked queue. The safe default is to leave them active and let the next session (on the new repo) either close and archive them deliberately or confirm they're dead. See `tasks.md §0` for the founder-decision checkbox.

## Risks / Trade-offs

### Risk 1 — Commit A accidentally includes a secret

**Scenario:** The staging sweep picks up a file the founder didn't intend to commit — a .env file, a private key, or a bcrypt hash.

**Mitigation:**
- Explicit `git add <path>` list, never `-A`/`.` (Decision 2)
- `git status --porcelain` inspection before commit
- Pre-commit grep for known secret patterns (`rg -e 'sk-[A-Za-z0-9]{32}' -e 'BEGIN.*PRIVATE KEY' <list of staged paths>`) before the commit command fires
- `.gitignore` already covers `.env`, `.env.local`, `*.pem`, `*.key`, `secrets.env`, `keys/` — verify coverage in tasks.md §1.1

**Blast radius if hit:** secret leaked on GitHub (private repo, but still visible to collaborators and GH logging). Remediation: revoke the leaked credential, force-purge via `git filter-repo`, force-push. High friction, avoid at all costs.

### Risk 2 — Wrong archive destination overwrites an existing archive

**Scenario:** The archive destination `<YYYY-MM-DD>-<name>/` collides with an existing subdirectory under `openspec/changes/archive/`.

**Mitigation:**
- Use today's date (2026-04-15). Existing archives use `2026-04-13-*` and a non-dated name. No collisions.
- Pre-move check: `test -d openspec/changes/archive/2026-04-15-<name> && echo "COLLISION" && exit 1`.

**Blast radius if hit:** `git mv` would fail or overwrite; in either case recoverable via `git reset`.

### Risk 3 — `gh repo create` fails because the name is taken

**Scenario:** `7inaydas-cmyk/Zovark_final` already exists (e.g., operator created it earlier).

**Mitigation:**
- Pre-create check: `gh repo view 7inaydas-cmyk/Zovark_final --json name 2>/dev/null && echo "EXISTS — confirm intent" && exit 2`.
- If it exists and is empty, operator can choose to proceed (treat as idempotent).
- If it exists and is non-empty, operator must decide: abort or delete-and-recreate (out of scope — abort).

**Blast radius:** zero if detected early. Proceeding blindly against a non-empty existing repo could clobber unrelated work.

### Risk 4 — Push fails mid-way (Commit A lands on local, but not on remote)

**Scenario:** Network failure after commit A, before/during `git push origin --all`.

**Mitigation:**
- Push is atomic per-branch but not across branches. If `audit/execution-fixes` pushes and `master` fails, we have a partial state.
- Recovery: rerun `git push origin --all` after fixing network. Idempotent.
- Gate 5 in tasks.md (`git log origin/<current-branch>..HEAD` must be empty) confirms tip match before proceeding.

**Blast radius:** recoverable; no data loss. Worst case is a second push invocation.

### Risk 5 — `git remote rename` done out of order leaves a dangling origin

**Scenario:** The operator runs `git remote add origin ...` BEFORE `git remote rename origin old-origin`. Git errors with "remote origin already exists" and the rename never happens.

**Mitigation:**
- tasks.md §3 sequences the commands: RENAME first (5.1), VERIFY rename (5.2), ADD new origin (5.3), VERIFY add (5.4).
- Alternative fix if the order gets flipped: `git remote rm origin` + `git remote rename old-origin origin` + retry. Recoverable.

**Blast radius:** zero, self-correcting with a clean-up.

### Trade-off — One big commit vs. topic-split commits

One "kitchen sink" commit is semantically ugly but has three advantages over splitting:
1. It's the real state of the session — everything touched is in one flight
2. It's atomic: if anything fails in verification we revert a single commit
3. Smaller risk of forgetting a file (explicit paths but one commit gate)

Splits would be: (a) stabilize-runtime-hygiene code, (b) session non-stabilize edits, (c) openspec artifacts, (d) archive moves. That's 4 commits, each needing its own message, each needing verification, each another chance for human error. Chose one commit for robustness.

### Trade-off — Force-push ban vs. convenience

Against an empty new repo, a force-push would never clobber anything. But the session rule is absolute: no force-push. The rule exists because the check is cheap and the risk of a stray force-push hitting the wrong remote (e.g., `old-origin`) is non-zero. Keep the ban.

## Migration Plan

Not applicable as a schema-level concept. The operational order is captured in `tasks.md`. Rollback procedure in `tasks.md §8`.

## Open Questions

Captured as founder decisions in `tasks.md §0`. Five decisions:
- F1: New repo name + namespace confirmation (`7inaydas-cmyk/Zovark_final`)
- F2: Commit author + identity — use existing git config (`7inaydas-cmyk <...@users.noreply.github.com>`)
- F3: Single commit A vs. split — single commit (Decision 1)
- F4: Scope of archive — 7 changes (6 complete + stabilize-runtime-hygiene), NOT the 4 nearly-complete ones
- F5: Whether to archive this change itself as part of commit A or commit B — Commit B (Decision 1)
