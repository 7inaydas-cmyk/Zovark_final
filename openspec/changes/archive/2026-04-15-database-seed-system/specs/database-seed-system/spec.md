## ADDED Requirements

### Requirement: Idempotent dev seed file
The repository SHALL ship a file at `migrations/seed_dev_data.sql` that, when executed against a freshly-migrated Zovark database, inserts a canonical dev tenant and two fixture users (admin and analyst). Every insert SHALL use `ON CONFLICT DO NOTHING` so the file is safe to run repeatedly. The file SHALL be wrapped in a single `BEGIN/COMMIT` transaction.

#### Scenario: First application against an empty DB
- **WHEN** the seed file is executed against a DB that has the `tenants` and `users` tables but no fixture data
- **THEN** one tenant row is inserted at id `00000000-0000-0000-0000-000000000010` and two user rows are inserted at ids `00000000-0000-0000-0000-000000000020` (admin) and `00000000-0000-0000-0000-000000000021` (analyst)

#### Scenario: Repeated application
- **WHEN** the seed file is executed a second time against a DB that already has the fixture rows
- **THEN** no rows are modified, no errors are raised, and the file exits cleanly

#### Scenario: Partial fixture (one row manually deleted)
- **WHEN** an operator runs `DELETE FROM users WHERE email = 'analyst2@test.local'` and then re-runs the seed file
- **THEN** the analyst user is re-inserted at its fixed UUID and the admin user is left untouched

### Requirement: Fixed UUIDs for seed entities
The dev tenant SHALL use the fixed UUID `00000000-0000-0000-0000-000000000010`. The admin user SHALL use `00000000-0000-0000-0000-000000000020`. The analyst user SHALL use `00000000-0000-0000-0000-000000000021`. These UUIDs SHALL be reserved for seed data and SHALL NOT be reused for real tenants or users.

#### Scenario: UUIDs are stable across runs
- **WHEN** the seed runs on two independent postgres volumes
- **THEN** both volumes have identical UUIDs for the dev tenant, admin user, and analyst user

### Requirement: Bcrypt cost-12 password hashes
The admin and analyst user password hashes SHALL be `$2b$12$…` bcrypt hashes — i.e., the hash prefix SHALL begin with `$2b$12$` or `$2a$12$`. The plaintext password for both users SHALL be `TestPass2026`.

#### Scenario: Hash cost verification
- **WHEN** the seed runs and a probe executes `SELECT substring(password_hash, 1, 7) FROM users WHERE email IN ('admin@test.local','analyst2@test.local')`
- **THEN** both rows return a value beginning with `$2b$12$` or `$2a$12$`

#### Scenario: Login with the literal password succeeds
- **WHEN** the API is running with a freshly-seeded DB and the caller POSTs `{"email":"admin@test.local","password":"TestPass2026"}` to `/api/v1/auth/login`
- **THEN** the response has HTTP 200 and contains a non-empty `token` field

### Requirement: Compose mount runs the seed on fresh boot
`docker-compose.yml` SHALL mount `migrations/seed_dev_data.sql` into the `postgres` container at `/docker-entrypoint-initdb.d/02-seed-dev.sql`. The file SHALL run automatically after `01-init.sql` on first boot of an empty postgres data volume, before the API container is healthy.

#### Scenario: Fresh volume first boot
- **WHEN** an operator runs `docker compose down -v && docker compose up -d` and waits for the API to reach `/ready`
- **THEN** the fixture users exist in the DB without any manual intervention

#### Scenario: Boot order
- **WHEN** the postgres container starts a fresh volume
- **THEN** `01-init.sql` runs before `02-seed-dev.sql`, and both complete before postgres accepts application connections

### Requirement: Manual re-seed helper
The repository SHALL ship `scripts/seed_dev.sh` as an executable (mode 0755) that, when invoked without arguments, executes the seed file against the running postgres container via `docker exec -i zovark-postgres psql`. It SHALL be safe to run repeatedly.

#### Scenario: Default invocation
- **WHEN** an operator runs `scripts/seed_dev.sh` against a running stack
- **THEN** the script executes `migrations/seed_dev_data.sql` inside the postgres container, prints a success message, and exits with status 0

#### Scenario: Second run
- **WHEN** the operator runs `scripts/seed_dev.sh` twice in a row
- **THEN** the second run completes successfully with the same output (idempotency guaranteed by the SQL file)

### Requirement: seed_dev.sh --check verifies fixture state
`scripts/seed_dev.sh --check` SHALL query the DB for the presence of the dev tenant and both fixture users and print a pass/fail table. It SHALL exit 0 when all three entities are present with bcrypt cost-12 hashes, exit 2 when at least one is present but not all, and exit 1 when the tenant itself is missing (indicating the seed never ran).

#### Scenario: Fully seeded DB
- **WHEN** `--check` runs against a DB with all three fixture rows present
- **THEN** the script exits 0 and the output shows `zovark-dev tenant: ✓`, `admin@test.local: ✓`, `analyst2@test.local: ✓`

#### Scenario: Admin user manually deleted
- **WHEN** `--check` runs against a DB where the admin row has been deleted
- **THEN** the script exits 2 and the output flags `admin@test.local: ✗` while the other rows stay green

#### Scenario: Fresh DB (seed never ran)
- **WHEN** `--check` runs against a DB with no dev tenant
- **THEN** the script exits 1 and the output flags every row as missing

### Requirement: seed_dev.sh --regenerate-hash prints rotation SQL
`scripts/seed_dev.sh --regenerate-hash` SHALL compute a fresh bcrypt cost-12 hash of the literal `TestPass2026` password via Python's `bcrypt` library and print an `UPDATE users SET password_hash = '…' WHERE email IN (…);` snippet to stdout. It SHALL NOT modify the running DB or the seed file.

#### Scenario: Hash generation
- **WHEN** `--regenerate-hash` runs on a host with Python 3 and the `bcrypt` package available
- **THEN** the script emits a single `UPDATE users SET password_hash = '$2b$12$…'` line and exits 0

#### Scenario: bcrypt not available
- **WHEN** `--regenerate-hash` runs on a host without Python bcrypt
- **THEN** the script emits an error message naming the missing dependency and exits 2

### Requirement: e2e probe uses seeded tenant via env fallback
`scripts/e2e_probe.sh` SHALL honor a new env var `ZOVARK_PROBE_TENANT_ID` (default `00000000-0000-0000-0000-000000000010`) that is used as a fallback tenant_id when the `/api/v1/auth/login` response does not include a `tenant_id` field. The primary path SHALL remain unchanged — the probe reads `tenant_id` from the login response first.

#### Scenario: Login returns tenant_id normally
- **WHEN** the probe runs and the login response contains `user.tenant_id`
- **THEN** the probe uses the response value, not the env fallback

#### Scenario: Login response missing tenant_id
- **WHEN** the probe runs and the login response has no `tenant_id` field
- **THEN** the probe falls back to `ZOVARK_PROBE_TENANT_ID` and continues

### Requirement: e2e probe --check-fixture flag
`scripts/e2e_probe.sh` SHALL accept a `--check-fixture` flag that logs in as `admin@test.local / TestPass2026`, asserts the response contains a valid token, and exits 0 without running the full pipeline probe. Exit 1 on login failure.

#### Scenario: Fixture user present
- **WHEN** `e2e_probe.sh --check-fixture` runs against a seeded DB
- **THEN** the script prints `fixture OK: token obtained for admin@test.local` and exits 0

#### Scenario: Fixture user missing
- **WHEN** `e2e_probe.sh --check-fixture` runs against an unseeded DB
- **THEN** the script prints the login error and exits 1

### Requirement: Stale 'Hydra Dev' tenant removed from init.sql
The `INSERT INTO tenants ('Hydra Dev', 'hydra-dev', 'enterprise')` statement SHALL be removed from `init.sql`. The canonical dev tenant SHALL come exclusively from `seed_dev_data.sql`.

#### Scenario: Grep for stale slug
- **WHEN** `git grep -i 'hydra-dev'` runs in the repo
- **THEN** no source code, script, or test references the stale slug
