## Why

Every time a developer or CI job wipes the postgres volume and runs `docker compose up -d`, the stack boots with **zero users**. The fixture credentials everyone assumes work (`admin@test.local` / `TestPass2026`, documented in `CLAUDE.md` and hardcoded into `scripts/e2e_probe.sh`, `scripts/smoke_test_100.sh`, and the dashboard's login page) are not actually inserted by any automated path. Today's workaround is a piece of operator tribal knowledge that says "manually psql a bcrypt hash into the users table after every fresh boot" — which, predictably, nobody does correctly, so half the time the e2e probe, the smoke test, and the dashboard all fail on first boot with 401s that look like real bugs.

The existing `init.sql` has a stale `INSERT INTO tenants` for "Hydra Dev" (still using the pre-rebrand name) and zero user rows. Migration `063_system_tenant.sql` creates a system tenant but no user attached to it. The new e2e probe (`scripts/e2e_probe.sh`) depends on a working login, so this gap makes the "single end-to-end verification" script fail at Stage 0 on any fresh volume — defeating the whole point.

**Fix**: make the seed data a first-class, deterministic, idempotent file that runs automatically after the migrations on every fresh boot, with a helper script for manual re-seeding when needed.

## What Changes

- **Add** `migrations/seed_dev_data.sql` — idempotent seed SQL that inserts:
  1. A canonical dev tenant at a **fixed UUID** (`00000000-0000-0000-0000-000000000010`), slug `zovark-dev`, tier `enterprise`, name `Zovark Dev` — the current rebranded name, not the stale `Hydra Dev`.
  2. The admin user (`admin@test.local` / `TestPass2026`) attached to that tenant at a **fixed UUID** (`00000000-0000-0000-0000-000000000020`) with role `admin`, `is_active=true`, and a **bcrypt cost-12 password hash** matching the audit 1.14 minimum. The hash is pre-computed at seed-file creation time and inlined so the SQL has zero runtime dependencies.
  3. A second analyst user (`analyst2@test.local` / `TestPass2026`) at `00000000-0000-0000-0000-000000000021` with role `analyst`, so tests that need a non-admin fixture don't fall back to creating one on the fly.
  - All three inserts use `ON CONFLICT DO NOTHING` so running the file twice is a no-op.
  - Wrapped in a single `BEGIN/COMMIT` transaction.
  - Heavy comment block at the top documenting the fixed UUIDs as "RESERVED — do not reuse" and the password as "DEV ONLY — rotate via `scripts/seed_dev.sh --regenerate-hash`".

- **Mount** `migrations/seed_dev_data.sql` into the postgres container at `/docker-entrypoint-initdb.d/02-seed-dev.sql` (alongside the existing `01-init.sql` mount), so Postgres' own entrypoint runs it on first boot of a fresh volume, AFTER `init.sql` has created the tables. Compose change is a single new volume line on the `postgres` service.

- **Add** `scripts/seed_dev.sh` — a manual re-seed helper for the case where an operator adds seed data AFTER the initial volume was created (when `docker-entrypoint-initdb.d` won't re-run) or when a developer wants to restore the fixture users after an accidental delete. Flags:
  - Default invocation: `docker exec -i zovark-postgres psql -U zovark -d zovark -v ON_ERROR_STOP=1 < migrations/seed_dev_data.sql`. Safe to run repeatedly because of the `ON CONFLICT DO NOTHING`.
  - `--check` prints a status table showing whether the seed tenant + both users exist (and what their bcrypt cost is, from the `$2b$12$` prefix).
  - `--regenerate-hash` generates a fresh bcrypt cost-12 hash for a given password and prints a ready-to-paste SQL `UPDATE` snippet operators can apply to rotate the fixture password. Uses `python3 -c 'import bcrypt; ...'` — bcrypt is already a worker dependency.
  - `--help` / `--no-color` / `--quiet` standard flags matching the rest of the scripts suite.
  - Strict shell: `set -euo pipefail`, `MSYS_NO_PATHCONV=1`, colored output, exit 0 on success / 1 on failure / 2 on degraded (e.g., one user missing).

- **Update** `scripts/e2e_probe.sh` to reference the seeded tenant:
  - Add a new env var `ZOVARK_PROBE_TENANT_ID` (default `00000000-0000-0000-0000-000000000010` — the seeded dev tenant) that is used as a fallback when the `/api/v1/auth/login` response doesn't return a `tenant_id` field.
  - The login path is still primary — the probe reads tenant_id from the login response first — but the fallback lets the probe survive a transient response-shape change without failing at Stage 0.
  - Add a `--check-fixture` flag that verifies the seeded user exists (by attempting a login) and exits 0 before the main probe runs. Useful for operators who want to validate the seed ran without executing the full e2e flow.

- **Remove the stale `INSERT INTO tenants` from `init.sql`** — the 'Hydra Dev' / `hydra-dev` slug is a rebrand leftover that creates two dev tenants if init.sql runs alongside the new seed file. Delete it cleanly; the canonical dev tenant comes from `seed_dev_data.sql`.

- **Docs**:
  - Update `CLAUDE.md` Credentials table: add a pointer to the seed file and note the fixed UUIDs.
  - Update `CLAUDE.md` "How to Run" section: remove any stale "run this manual psql command first" steps; the seed now runs automatically.
  - New section in `docs/RUNBOOK_HEALTHCHECK.md` ("Fixture users missing after upgrade") with copy-paste diagnostic and remediation commands (run `scripts/seed_dev.sh`, then `scripts/seed_dev.sh --check` to verify).

- **CI**: `.github/workflows/ci.yml` integration test job already depends on the fixture user. We add a `scripts/seed_dev.sh --check` step after `docker compose up` and before the smoke tests, so CI fails fast with a clear error message if the seed didn't land.

## Capabilities

### New Capabilities

- `database-seed-system`: A deterministic, idempotent seed path for dev/CI fixture data — canonical tenant + two users with fixed UUIDs and bcrypt cost-12 passwords — plus a helper script for manual re-seeding and verification. Makes `admin@test.local`/`TestPass2026` a guaranteed first-class fixture across compose, CI, the e2e probe, the smoke test, and the dashboard login page.

### Modified Capabilities

- `e2e-pipeline-probe`: adds `ZOVARK_PROBE_TENANT_ID` env fallback and `--check-fixture` flag (see design D4). Default behaviour is unchanged — the probe still reads tenant_id from the login response first.

## Impact

- **Affected code**: new `migrations/seed_dev_data.sql`, new `scripts/seed_dev.sh`, one-line edit to `docker-compose.yml` (new mount), small edits to `scripts/e2e_probe.sh` (env fallback + optional `--check-fixture` flag), stale-tenant removal from `init.sql`, doc updates, one new CI step in `.github/workflows/ci.yml`.
- **Data impact on existing dev DBs**: zero. `ON CONFLICT DO NOTHING` means running the seed against a DB that already has the admin user (manually inserted) leaves the row untouched. The seed is additive.
- **Fresh-boot impact**: every `docker compose up -d` on a fresh postgres volume now has the admin + analyst users from the moment the API starts accepting connections. No manual step, no race.
- **Dependencies**: `bcrypt` Python package for `scripts/seed_dev.sh --regenerate-hash` — already a worker dependency; we do not pull it into the image separately.
- **Risk**: low. The seed is idempotent, runs inside a transaction, uses `ON CONFLICT DO NOTHING`, and operates only on the two fixture UUIDs (`…0010`, `…0020`, `…0021`) which are reserved for seed data. No existing dev tenant can collide.
- **Breaking**: **minor** — removing the `INSERT INTO tenants ('Hydra Dev', 'hydra-dev', ...)` from `init.sql` means a fresh boot no longer has a tenant named `Hydra Dev`. Any test fixture that hard-codes the `hydra-dev` slug breaks. Migration step: grep the repo for `hydra-dev` and update to `zovark-dev`. Existing deployments already have the tenant (so no impact on upgrade).
