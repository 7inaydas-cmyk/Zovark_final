## 1. Generate the bcrypt hash

- [x] 1.1 Run `python3 -c 'import bcrypt; print(bcrypt.hashpw(b"TestPass2026", bcrypt.gensalt(12)).decode())'` on an implementation host with Python bcrypt available
- [x] 1.2 Record the resulting `$2b$12$...` literal for paste into the seed file; confirm the prefix is `$2b$12$` (cost 12)
- [x] 1.3 Round-trip verify: `python3 -c "import bcrypt; print(bcrypt.checkpw(b'TestPass2026', b'<hash>'))"` prints `True`

## 2. migrations/seed_dev_data.sql

- [x] 2.1 Create `migrations/seed_dev_data.sql` with a heavy comment header documenting: purpose, fixed UUIDs, idempotency, and the `scripts/seed_dev.sh --regenerate-hash` rotation path
- [x] 2.2 Wrap the entire file in a single `BEGIN;` / `COMMIT;` transaction
- [x] 2.3 `INSERT INTO tenants (id, name, slug, tier, is_active, created_at, updated_at) VALUES ('00000000-0000-0000-0000-000000000010', 'Zovark Dev', 'zovark-dev', 'enterprise', true, NOW(), NOW()) ON CONFLICT (id) DO NOTHING;`
- [x] 2.4 `INSERT INTO users (id, tenant_id, email, display_name, password_hash, role, is_active, created_at, updated_at) VALUES ('00000000-0000-0000-0000-000000000020', '00000000-0000-0000-0000-000000000010', 'admin@test.local', 'Admin', '<bcrypt hash from §1>', 'admin', true, NOW(), NOW()) ON CONFLICT (id) DO NOTHING;`
- [x] 2.5 Insert the analyst user at `00000000-0000-0000-0000-000000000021` with email `analyst2@test.local`, display name `Analyst 2`, role `analyst`, same bcrypt hash, and `ON CONFLICT (id) DO NOTHING`
- [x] 2.6 Validate the file via `docker run --rm -v $(pwd):/w -w /w postgres:16 psql -U postgres -c '\i migrations/seed_dev_data.sql' --dry-run` (or equivalent syntax-only check; fallback is `psql -f … -- --dry-run`)

## 3. docker-compose.yml mount

- [x] 3.1 Edit `docker-compose.yml` postgres service volumes: add `./migrations/seed_dev_data.sql:/docker-entrypoint-initdb.d/02-seed-dev.sql:ro`
- [x] 3.2 Verify YAML parse after edit via `python3 -c "import yaml; yaml.safe_load(open('docker-compose.yml'))"`
- [x] 3.3 Confirm the mount path is alphabetically AFTER the existing `/docker-entrypoint-initdb.d/01-init.sql` mount so boot order is init → seed

## 4. scripts/seed_dev.sh

- [x] 4.1 Create `scripts/seed_dev.sh` with `#!/usr/bin/env bash`, `set -euo pipefail`, `MSYS_NO_PATHCONV=1`, and a header comment documenting purpose, flags, exit codes, env vars
- [x] 4.2 `chmod +x scripts/seed_dev.sh`
- [x] 4.3 Parse flags: `--check`, `--regenerate-hash`, `--no-color`, `--quiet`, `--help`
- [x] 4.4 Implement TTY-aware color helpers reusing the same pattern as `stack_healthcheck.sh` and `e2e_probe.sh`
- [x] 4.5 Default invocation: resolve the postgres container (via `docker compose ps -q postgres`, fallback `docker ps --filter name=zovark-postgres`), then `docker exec -i <container> psql -U zovark -d zovark -v ON_ERROR_STOP=1 < migrations/seed_dev_data.sql`. Print pass/fail result.
- [x] 4.6 `--check`: query the DB for the three fixture rows and print a table:
        ```
        tenant zovark-dev (…0010):            ✓/✗
        user admin@test.local (…0020):       ✓/✗  [cost=12]
        user analyst2@test.local (…0021):    ✓/✗  [cost=12]
        ```
        Exit 0 if all present with cost 12, 2 if partial, 1 if tenant missing.
- [x] 4.7 `--regenerate-hash`: invoke `python3 -c 'import bcrypt; print(bcrypt.hashpw(b"TestPass2026", bcrypt.gensalt(12)).decode())'`, capture the output, and emit a ready-to-paste `UPDATE users SET password_hash = '$2b$12$…' WHERE email IN ('admin@test.local', 'analyst2@test.local');` snippet to stdout. Exit 2 if Python or bcrypt is missing.
- [x] 4.8 `--help` prints usage documenting every flag and env var

## 5. scripts/e2e_probe.sh updates

- [x] 5.1 Add env var `ZOVARK_PROBE_TENANT_ID="${ZOVARK_PROBE_TENANT_ID:-00000000-0000-0000-0000-000000000010}"` in the Defaults block
- [x] 5.2 In Stage 0, when extracting `TENANT_ID` from the login response, fall back to `$ZOVARK_PROBE_TENANT_ID` if the response's `.user.tenant_id // .tenant_id` is empty
- [x] 5.3 Add `--check-fixture` flag to the CLI parser
- [x] 5.4 When `--check-fixture` is set, perform ONLY the login and an assertion on the returned token, print `fixture OK: token obtained for admin@test.local` on success, then exit 0 (skip all other stages)
- [x] 5.5 Update the probe's `--help` to document `--check-fixture` and `ZOVARK_PROBE_TENANT_ID`

## 6. init.sql cleanup

- [x] 6.1 Remove the `INSERT INTO tenants (name, slug, tier) VALUES ('Hydra Dev', 'hydra-dev', 'enterprise') ON CONFLICT (slug) DO NOTHING;` block from `init.sql` around line 411
- [x] 6.2 `git grep -i "hydra-dev\|Hydra Dev"` across the repo; rewrite any remaining references to `zovark-dev` / `Zovark Dev` in the same PR
- [x] 6.3 Confirm `init.sql` still parses by doing a dry psql check inside a throwaway postgres container (or at minimum `python3 -c "print(open('init.sql').read()[:100])"` + eyeball)

## 7. Docs

- [x] 7.1 Update `CLAUDE.md` Credentials table: note that the admin and analyst users are seeded automatically by `migrations/seed_dev_data.sql` and reference the fixed UUIDs
- [x] 7.2 Update `CLAUDE.md` "How to Run" section: remove any stale manual seed steps; point at `scripts/seed_dev.sh` for manual re-seeding
- [x] 7.3 Add a "Fixture users" section to `docs/RUNBOOK_HEALTHCHECK.md` under the healer/e2e content: include the full diagnostic flow — `scripts/seed_dev.sh --check` → `scripts/seed_dev.sh` → re-check → run `scripts/e2e_probe.sh --check-fixture` to confirm end-to-end
- [x] 7.4 Document the manual DELETE command (wipe fixture users) in the runbook for operators who need to reset

## 8. CI integration

- [x] 8.1 Add a `Verify fixture users seeded` step to `.github/workflows/ci.yml` in the integration job, placed immediately after the existing "Wait for API health" step
- [x] 8.2 The step runs `scripts/seed_dev.sh --check` and fails the job on non-zero exit
- [x] 8.3 Validate the CI yaml via `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`

## 9. Verification

- [x] 9.1 `bash -n scripts/seed_dev.sh` exits 0
- [x] 9.2 `scripts/seed_dev.sh --help` prints usage and exits 0
- [x] 9.3 `test -x scripts/seed_dev.sh` is true
- [x] 9.4 `python3 -c "import yaml; yaml.safe_load(open('docker-compose.yml'))"` succeeds
- [x] 9.5 `scripts/seed_dev.sh --regenerate-hash` (when Python bcrypt is available) emits a line starting with `UPDATE users SET password_hash = '$2b$12$` and containing both fixture emails
- [x] 9.6 `scripts/seed_dev.sh --regenerate-hash` on a host without Python bcrypt emits an informative error and exits 2
- [x] 9.7 Dry-run the seed SQL against a throwaway postgres (via `docker run` or equivalent) and confirm no errors
- [x] 9.8 Manual live-stack run: `docker compose down -v && docker compose up -d && scripts/seed_dev.sh --check && scripts/e2e_probe.sh --check-fixture` — all three commands exit 0 in sequence
- [x] 9.9 Second-run idempotency: re-run `scripts/seed_dev.sh` and confirm exit 0 with no changes
- [x] 9.10 Grep sweep post-merge: `git grep -i 'hydra-dev\|Hydra Dev'` returns empty (no stragglers)
