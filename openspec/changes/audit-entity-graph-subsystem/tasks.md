## 1. Audit Deliverable

- [ ] 1.1 Write `docs/AUDIT_ENTITY_GRAPH.md` with the full table/view/index inventory, the writer/reader map, and every inconsistency found (file:line refs for `migrations/068`, `init.sql:485-696`, `worker/intelligence/cross_tenant.py:30,79`, `worker/intelligence/stix_taxii.py:112`, `worker/embedding/batch.py:90`, `worker/embedding/versioning.py:157`, `api/entity_graph_handlers.go`, `api/cross_tenant_handlers.go`, `api/gdpr.go:24-40`).
- [ ] 1.2 Add a "Resolution" section to the audit doc that links each finding to the task number below that fixes it.
- [ ] 1.3 Update the "Database" and "Known Issues" sections of `CLAUDE.md` to point at the audit doc and remove now-resolved items.

## 2. Schema Restoration Migration

- [ ] 2.1 Create `migrations/072_entity_graph_restoration.sql` that `CREATE TABLE IF NOT EXISTS` for `entities`, `entity_edges`, `entity_observations` matching `init.sql:485-556` exactly (columns, CHECK constraints, indexes).
- [ ] 2.2 In the same migration, `CREATE MATERIALIZED VIEW IF NOT EXISTS cross_tenant_intel …` and `CREATE OR REPLACE VIEW cross_tenant_public …` matching `migrations/006_sprint1k_cross_tenant.sql`.
- [ ] 2.3 In the same migration, add FK constraints `entity_edges.investigation_id → investigations(id) ON DELETE CASCADE` and `entity_observations.investigation_id → investigations(id) ON DELETE CASCADE`, guarded with a `DO $$ BEGIN … EXCEPTION WHEN duplicate_object THEN NULL; END $$;` block so re-runs are idempotent.
- [ ] 2.4 Apply the migration on a fresh dev DB (`docker compose down -v && docker compose up -d && docker compose exec -T postgres psql -U zovark -d zovark < migrations/072_entity_graph_restoration.sql`) and verify it is a no-op (no errors, no row changes).
- [ ] 2.5 Apply the migration on a dev DB where `068` was applied first; verify the four tables and two views exist afterward.
- [ ] 2.6 Run `pg_dump --schema-only` of the entity-graph objects on both databases and confirm they are byte-equivalent (satisfies the "Migrations and init.sql agree on the entity schema" requirement).

## 3. Cross-Tenant Activity Rewrite

- [ ] 3.1 Rewrite `refresh_cross_tenant_intel` in `worker/intelligence/cross_tenant.py` to recompute `cross_tenant_entities` directly from `entities + entity_observations + investigations` via `INSERT … ON CONFLICT (entity_hash) DO UPDATE`, in a single transaction.
- [ ] 3.2 In the same function, update `entities.tenant_count` from a CTE on `entity_observations`, with no view dependency.
- [ ] 3.3 After the recompute, probe `pg_matviews` for `cross_tenant_intel`; if present, `REFRESH MATERIALIZED VIEW CONCURRENTLY cross_tenant_intel`. If absent, log and continue.
- [ ] 3.4 Rewrite `get_entity_intelligence` (currently at `worker/intelligence/cross_tenant.py:79-85`) to query `cross_tenant_entities` (real table) instead of `cross_tenant_public` (view).
- [ ] 3.5 Add a unit test `worker/tests/test_cross_tenant.py::test_refresh_succeeds_when_view_missing` that drops the view and asserts the activity completes without error.
- [ ] 3.6 Run `cross_tenant_workflow` end-to-end on a seeded dev DB; confirm `cross_tenant_entities` populates and `entities.tenant_count` updates.

## 4. STIX/TAXII Writer Fix

- [ ] 4.1 Add `stix_taxii_enabled: bool = False` (env: `ZOVARK_STIX_TAXII_ENABLED`) to `worker/settings.py`.
- [ ] 4.2 Make `ingest_threat_feed` in `worker/intelligence/stix_taxii.py` short-circuit return when the flag is false, before opening any database connection.
- [ ] 4.3 Replace the broken `INSERT INTO entities (value, type, …)` at `worker/intelligence/stix_taxii.py:112` with the canonical column list `(entity_hash, entity_type, value, tenant_id, first_seen, last_seen, threat_score, metadata)` and `ON CONFLICT (entity_hash, tenant_id) DO UPDATE`.
- [ ] 4.4 Compute `entity_hash = hashlib.sha256(f"{entity_type}:{value}".encode()).hexdigest()` to match the convention used in `worker/entity_graph.py`.
- [ ] 4.5 Replace the existing `STIX_TYPE_MAP` with a function that parses the STIX `pattern` field for indicators and emits `entity_type` values from the CHECK domain (`ip`, `domain`, `file_hash`, `url`); skip `malware`/`threat-actor`/`tool`/`attack-pattern` objects entirely with a counter increment.
- [ ] 4.6 Add `worker/tests/test_stix_taxii.py::test_insert_sql_parses_against_live_schema` that opens a transaction, runs the new INSERT against the real schema with a synthetic indicator, and rolls back. Catches column-name regressions in CI.
- [ ] 4.7 Add `worker/tests/test_stix_taxii.py::test_malware_object_skipped` that asserts a STIX `malware` input does not produce an `entities` row.

## 5. Embedding Writer Removal

- [ ] 5.1 Delete the `UPDATE entities SET embedding = …::vector WHERE id = …` block in `worker/embedding/batch.py:88-92` and any surrounding code that exists only to feed it.
- [ ] 5.2 Delete the equivalent block in `worker/embedding/versioning.py:157`.
- [ ] 5.3 If `worker/embedding/batch.py` has no remaining callers after the deletion, delete the module entirely. Same for `versioning.py`.
- [ ] 5.4 Run `grep -rn "SET embedding" worker/` and `grep -rn "::vector" worker/` and confirm both return zero matches.
- [ ] 5.5 Run `python -m py_compile $(find worker -name '*.py')` to confirm nothing imports the deleted symbols.

## 6. Init.sql Reconciliation

- [ ] 6.1 Walk `init.sql:485-696` line-by-line against `migrations/072_entity_graph_restoration.sql` and any column/index/CHECK divergence — fix `init.sql` so the two are byte-equivalent for the entity-graph objects.
- [ ] 6.2 Add a header comment block to `init.sql` near line 485 explaining that the entity-graph schema is mirrored in `migrations/072_entity_graph_restoration.sql` and that any future change must edit both.
- [ ] 6.3 Re-run the `pg_dump --schema-only` diff from task 2.6 and confirm it stays empty.

## 7. API Read Path Verification

- [ ] 7.1 With a seeded dev DB, call `GET /api/v1/entities?type=ip` and confirm a non-empty response.
- [ ] 7.2 Call `GET /api/v1/entities/<id>/neighborhood` for an entity with known edges and confirm at least one edge is returned.
- [ ] 7.3 Call `GET /api/v1/intelligence/top-threats` and `GET /api/v1/intelligence/stats` and confirm `total_entities > 0`.
- [ ] 7.4 Verify tenant scoping: log in as analyst from tenant B and confirm `GET /api/v1/entities` does not return tenant A's rows.

## 8. GDPR Erasure Verification

- [ ] 8.1 Seed a tenant with 5+ entities, 10+ edges, 20+ observations.
- [ ] 8.2 Call `DELETE /api/v1/tenants/<tenant_id>/data` and confirm row counts go to zero in all three tables for that tenant.
- [ ] 8.3 Insert a fresh investigation, then `DELETE FROM investigations WHERE id = …` and confirm `entity_edges` and `entity_observations` rows for that investigation are removed by the new FK CASCADE (not by application code).

## 9. Test Coverage

- [ ] 9.1 Add `tests/integration/test_entity_graph_e2e.py`: submit a synthetic alert via `POST /api/v1/tasks` (e.g., brute_force with source_ip `185.220.101.45`), poll for completion, then assert the IP appears in `GET /api/v1/entities?type=ip` for the same tenant.
- [ ] 9.2 In the same test, fetch the entity by id and call `GET /api/v1/entities/<id>/neighborhood`, asserting at least one edge.
- [ ] 9.3 Add `api/entity_graph_handlers_test.go` with two tests: one that seeds two `entities` + one `entity_edges` row via the test DB pool and asserts `listEntityGraphHandler` returns the expected JSON shape, and one that asserts `entityNeighborhoodHandler` returns the seed plus its neighbour.
- [ ] 9.4 Wire both new test files into `.github/workflows/ci.yml` (Python integration job + Go unit job).

## 10. Documentation and Sign-Off

- [ ] 10.1 Update `CLAUDE.md` "Database" section to list `entities`, `entity_edges`, `entity_observations`, `cross_tenant_entities` explicitly and reference `migrations/072_entity_graph_restoration.sql`.
- [ ] 10.2 Update `CLAUDE.md` "Known Issues" section to remove any items resolved by this change and add a single line documenting that STIX ingest is gated behind `ZOVARK_STIX_TAXII_ENABLED`.
- [ ] 10.3 Update `CLAUDE.md` "Environment Variables" table with the new `ZOVARK_STIX_TAXII_ENABLED` flag.
- [ ] 10.4 Cross-link `docs/AUDIT_ENTITY_GRAPH.md` from `docs/ARCHITECTURE.md`.
- [ ] 10.5 Run the full smoke test suite (`scripts/smoke_test_100.sh`) and confirm 100% pass rate (no entity-graph regressions).
