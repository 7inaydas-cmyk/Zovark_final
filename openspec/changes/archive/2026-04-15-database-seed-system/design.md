## Context

The Zovark stack has three overlapping sources of schema + seed data:

1. **`init.sql`** (1503 lines) — mounted at `/docker-entrypoint-initdb.d/01-init.sql`. Runs on a fresh postgres volume, creates every table, inserts one stale tenant (`'Hydra Dev'` — pre-rebrand name).
2. **`migrations/*.sql`** (64 files + the audit's new 071) — applied by an out-of-band migrator. Currently NOT wired into docker-compose.
3. **Operator tribal knowledge** — "insert a fixture user manually via psql". Never automated, never documented in the runbook, never consistent across contributors.

The audit-round-2-fixes change tracks deleting `init.sql` entirely and making migrations the single source of truth (D5 in that change). That's the long-term target. In the meantime, this change needs to produce a deterministic fixture user path that works TODAY, with `init.sql` still in place, AND survives the eventual `init.sql` deletion.

The three downstream consumers of the fixture user are:

- **`scripts/e2e_probe.sh`** — logs in as `admin@test.local / TestPass2026` at Stage 0. Without this user, the probe fails before it starts.
- **`scripts/smoke_test_100.sh`** — same login.
- **Dashboard login page** — the "Login as admin" hint in `dashboard/src/pages/Login.tsx` says to use the same credentials.

All three are broken on any fresh boot of any fresh volume. This change makes them green automatically.

Stakeholders: every contributor; CI (pre-deploy fixture sanity); every operator who runs `docker compose up -d`.

## Goals / Non-Goals

**Goals:**
- **Zero manual SQL for the fixture user**. Fresh `docker compose up -d` produces a working login on first API accept.
- **Fixed UUIDs** so tests, scripts, and dashboard code can reference them as literals instead of querying by slug/email. UUIDs chosen from an obviously-reserved range (`00000000-0000-0000-0000-0000000000Nx`) so they can't collide with real data.
- **Idempotent**. `psql < seed_dev_data.sql` is safe to run any number of times. `ON CONFLICT DO NOTHING` on every insert.
- **Compatible with both the current `init.sql + migrations` boot path AND the eventual migrations-only boot path** from audit-round-2-fixes D5. The seed file is self-contained and doesn't depend on `init.sql` at all.
- **Bcrypt cost 12** for the password hash, matching the audit 1.14 minimum. No downgrade for dev convenience.
- **One manual re-seed helper** (`scripts/seed_dev.sh`) for the case where the entrypoint didn't run (non-fresh volume), with a `--check` flag that verifies fixture state.
- **Runbook section** so the next operator who hits "fixture user missing" has a single command to run.
- **CI integration** that fails loud when the seed doesn't land.

**Non-Goals:**
- Not producing a prod seed path. Real customer tenants are provisioned via a different flow (OIDC JIT + break-glass auth). This change is dev/CI only.
- Not replacing the Go `migrate` subcommand or the `apply_migrations.sh` shell script. Those still own real migrations; the seed is a sibling file.
- Not adding runtime seed validation to the Go API. The API already rejects invalid logins with 401; a missing fixture is diagnosed by the script or by e2e_probe's Stage 0 failure.
- Not touching the dashboard login page. The credentials it hints at are already correct; this change just makes them actually work.
- Not adding a second "test-only" fixture tenant for multi-tenant tests. The `system` tenant from mig 063 plus this change's `zovark-dev` tenant are enough for every current test; additional tenants are a follow-up if multi-tenant isolation tests need them.
- Not migrating the existing `'Hydra Dev'` tenant data. The rebrand already renamed containers, Docker images, and env vars; the stale tenant slug is a pure cleanup, nothing references it.
- Not rotating the dev password or putting it behind Vault. `TestPass2026` stays a literal in the SQL and in CLAUDE.md, same as today, same as every other dev framework. Production deployments get their admin user via OIDC, not via this seed.

## Decisions

### D1 — Fixed reserved UUIDs instead of random UUIDs
```
Tenant:       00000000-0000-0000-0000-000000000010  (zovark-dev)
Admin user:   00000000-0000-0000-0000-000000000020  (admin@test.local)
Analyst user: 00000000-0000-0000-0000-000000000021  (analyst2@test.local)
```

Random UUIDs would force every test and every script to first `SELECT id FROM users WHERE email = ...` before doing anything. Fixed UUIDs let tests reference the fixtures as literals (`where tenant_id = '00000000-0000-0000-0000-000000000010'`) and let the e2e probe fall back to the env default.

**Alternative considered**: UUIDv5 hashes from a namespace. Rejected — less readable and not actually deterministic across psql vs Python implementations.

**Alternative considered**: let the DB generate IDs and export them via `\copy … to stdout`. Rejected — adds an out-of-band coupling between the seed file and whoever reads it.

The `…0010` / `…0020` / `…0021` pattern sits in an obviously-synthetic range — `00000000-0000-0000-0000-000000000001` is the `SYSTEM` tenant from mig 063, so the reserved range is already well-established.

### D2 — Pre-computed bcrypt hash, not runtime generation
Postgres doesn't ship with a bcrypt function (`pgcrypto.crypt()` supports `gen_salt('bf')` which is bcrypt — but cost is limited to 31, Python bcrypt and Postgres pgcrypto use compatible $2a$/$2b$ formats, so technically we COULD use `crypt(password, gen_salt('bf', 12))`).

However, we explicitly want the hash to be a single literal string in the SQL file so:
- The seed file has zero Postgres extension dependencies.
- The hash is reviewable in diffs (anyone can check `$2b$12$...` prefix).
- `scripts/seed_dev.sh --regenerate-hash` computes via Python bcrypt (same algorithm, same cost), confirms round-trip compat, and emits the replacement line.

The SQL hash will be generated at implementation time via `python3 -c 'import bcrypt; print(bcrypt.hashpw(b"TestPass2026", bcrypt.gensalt(12)).decode())'` and pasted as a literal. To rotate, run `scripts/seed_dev.sh --regenerate-hash` which prints the `UPDATE users SET password_hash = '$2b$12$…' WHERE email = 'admin@test.local';` snippet.

**Alternative considered**: use `pgcrypto` at seed time (`crypt('TestPass2026', gen_salt('bf', 12))`). Rejected — the seed file would depend on `CREATE EXTENSION IF NOT EXISTS pgcrypto`, which is a runtime side effect we'd rather not introduce from a seed file.

### D3 — Mount as `/docker-entrypoint-initdb.d/02-seed-dev.sql`, alphabetically after init.sql
Postgres' own entrypoint runs every `*.sql` / `*.sh` in `/docker-entrypoint-initdb.d/` alphabetically on first boot of an empty data directory. Mounting as `02-seed-dev.sql` guarantees it runs AFTER `01-init.sql` (so the `tenants` and `users` tables exist) and before the API container starts accepting connections (because postgres' healthcheck waits until init is done). No changes to the migrator or the compose health-ordering.

**Alternative considered**: run the seed as part of `apply_migrations.sh`. Rejected — the audit-round-2-fixes batch is redesigning `apply_migrations.sh` to use a ledger table and transactions, and that rework is out of scope here. Keeping the seed in `docker-entrypoint-initdb.d` is the simplest path that works today and survives the squash-migration cutover (it'll just become `01-seed-dev.sql` when init.sql goes away).

### D4 — `scripts/e2e_probe.sh` gains env fallback + `--check-fixture`
The probe currently extracts `tenant_id` from the login response at Stage 0. Add two small changes:
- `ZOVARK_PROBE_TENANT_ID="${ZOVARK_PROBE_TENANT_ID:-00000000-0000-0000-0000-000000000010}"` — fallback used ONLY if the login response is missing the field (e.g. API returns a different shape on a future upgrade). Primary path is unchanged.
- `--check-fixture` flag: runs just the login call, verifies the user exists, prints `OK` and exits 0. Use-case: CI step that wants to fail fast on missing seed without burning the full probe budget. `scripts/seed_dev.sh --check` is the DB-level equivalent; `--check-fixture` is the API-level equivalent.

### D5 — `scripts/seed_dev.sh --check` queries the DB directly
The `--check` flag verifies:
1. Dev tenant exists at the fixed UUID.
2. Admin user exists with a `$2b$12$` password hash (asserts cost 12).
3. Analyst user exists with a `$2b$12$` password hash.

Returns a colored pass/fail table. Exit 0 if all three are present, 2 if only some are present (partial fixture — probably after a manual DELETE), 1 if the tenant is missing (the seed never ran). The runbook points at this as the first diagnostic.

### D6 — `scripts/seed_dev.sh --regenerate-hash`
Takes no argument (uses `TestPass2026` as the literal dev password) and prints:
```
-- Replace the existing hash in migrations/seed_dev_data.sql with:
UPDATE users SET password_hash = '$2b$12$…'
 WHERE email IN ('admin@test.local', 'analyst2@test.local');
```
The operator copy-pastes the `UPDATE` into psql to rotate an existing DB, and separately updates `seed_dev_data.sql` with the new literal so fresh boots pick it up. Two-step by design — we don't want a single command that silently changes the seed file AND the running DB, because that masks drift.

### D7 — Remove the stale `INSERT INTO tenants ('Hydra Dev', ...)` from `init.sql`
The `hydra-dev` slug is a pre-rebrand leftover that nobody references. Grep confirms no code, script, or test queries for it. Delete the lines cleanly. The new seed file creates the canonical `zovark-dev` tenant at a fixed UUID, which is what everything should use going forward.

**Risk**: any test fixture that hard-codes `hydra-dev` breaks. Mitigation: grep the entire repo as part of the implementation; if anything matches, update it to `zovark-dev` in the same PR.

### D8 — CI integration
`.github/workflows/ci.yml`'s "Build and start stack" step already runs `docker compose up -d --build`. We add a new step immediately after "Wait for API health":
```yaml
- name: Verify fixture users seeded
  run: scripts/seed_dev.sh --check
```
Fails the job with `exit 1` if the seed didn't land — operators see "fixture missing" in CI output instead of a misleading "authentication failed" downstream.

### D9 — Runbook section: "Fixture users missing after upgrade"
When an operator upgrades the stack and the seed didn't run (because the volume was pre-existing), the runbook now has a one-command fix: `scripts/seed_dev.sh`. Pair with a `--check` to verify. Section in `docs/RUNBOOK_HEALTHCHECK.md` under a new "Fixture users" heading.

### D10 — Keep `TestPass2026` as the literal dev password
Dev environments need a known-good password. Rotating it in dev adds zero security (the DB has `hydra_dev_2026` as the DB password anyway). Production admins are provisioned via OIDC JIT, not this seed. `CLAUDE.md`'s "Credentials" table already documents this.

## Risks / Trade-offs

- **[Risk] Operators upgrading an existing volume never see the new seed** because `docker-entrypoint-initdb.d` only runs on first boot. → Mitigation: `scripts/seed_dev.sh` exists for exactly this case, and the runbook section documents it. CI's `--check` step catches it before tests run.
- **[Risk] The inlined bcrypt hash rots if anyone regenerates it without updating the seed file.** → Mitigation: `--regenerate-hash` explicitly prints a two-step workflow — update the DB AND update the file. The runbook repeats this.
- **[Risk] Removing the stale `Hydra Dev` insert from `init.sql` breaks a test fixture we didn't grep for.** → Mitigation: grep sweep is part of the task list (§7.4). If anything matches, fix it in the same PR.
- **[Risk] Fixed UUIDs in the `00000000-0000-0000-0000-0000000000Nx` range collide with a future "reserved" slot someone else claims.** → Mitigation: the range is already half-used (`…0001` = system). Document `…0010` – `…00FF` as "reserved for dev seed + tests" in the seed file header.
- **[Risk] CI `--check` step fails on the first run after merging this change because the existing CI volume is pre-populated.** → Mitigation: CI should always start with a fresh volume (docker compose down -v in the previous step); this is already the case in `ci.yml`. If not, a one-time manual volume wipe fixes it.
- **[Risk] Dev laptops with long-lived postgres volumes need a manual seed step after pulling this change.** → Mitigation: README note + first-run hint in the runbook; the manual step is just `scripts/seed_dev.sh`.
- **[Trade-off] Fixed UUIDs and literal password in source control.** → Accepted. Every dev framework in existence does this. The `TestPass2026` literal is already in `CLAUDE.md`, the dashboard login hint, and the smoke test script. We're not adding exposure, we're making it deterministic.
- **[Trade-off] The seed file can't be auto-versioned like real migrations (no ledger entry).** → Accepted. It's idempotent and runs via `docker-entrypoint-initdb.d`, which has its own versioning (the data directory). Not a real migration, shouldn't be tracked as one.

## Migration Plan

1. **Single PR, no flags**: land `migrations/seed_dev_data.sql`, `scripts/seed_dev.sh`, the compose mount, the e2e probe env fallback, the init.sql cleanup, docs, and CI step in one change. All edits are additive or stale-removal.
2. **Pre-apply sanity**: grep the repo for `hydra-dev` and `Hydra Dev` (case-insensitive). If anything matches, rewrite to `zovark-dev` / `Zovark Dev` in the same PR.
3. **CI validation**: the `--check` step runs in the integration job on the very first PR that includes it. Failure there catches any typo or mount-path mismatch before the change lands.
4. **Rollout**: `docker compose down -v && docker compose up -d` on a dev laptop reseeds automatically. Operators with pre-existing volumes run `scripts/seed_dev.sh` once.
5. **Rollback**: `git revert`. Zero runtime impact because the seed is idempotent — reverting doesn't delete the rows, it just stops creating them on future fresh boots. Operators who need to purge the fixture run a one-line DELETE.

## Open Questions

- **Q1**: Should the seed file be moved out of `migrations/` so it's not confused with real migrations? → **Recommendation**: no, keep it at `migrations/seed_dev_data.sql`. It lives with the DDL, operators already know to look there, and the filename prefix (`seed_`) disambiguates it from numbered migrations.
- **Q2**: Should we seed a default agent_skill / investigation_plan alongside the user? → **Recommendation**: no. The investigation plans are already in `worker/tools/investigation_plans.json` — the worker doesn't need a DB seed for that. Keep the seed focused on auth fixtures.
- **Q3**: Should the analyst user match the name in `CLAUDE.md` (`analyst2@test.local`) or use `analyst@test.local`? → **Recommendation**: `analyst2@test.local` to match `CLAUDE.md`. Parity with existing docs beats brevity.
- **Q4**: Should `scripts/seed_dev.sh` also support a `--wipe` flag that deletes the fixture users? → **Recommendation**: no. A one-line psql DELETE is simpler and less likely to be misused than a flag that makes it look endorsed. Document the DELETE command in the runbook.
- **Q5**: Should the CI `--check` step happen before the "Wait for API health" step or after? → **Recommendation**: after — the check needs the API to be up (at least to make a login call if we choose to use the API path), and it's a cheap call compared to `psql --tuples-only`. Actually, `--check` queries the DB directly, not the API, so it could technically run before API health. Recommendation stands: run after API health for simplicity of ordering.
