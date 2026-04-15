## ADDED Requirements

### Requirement: Canonical entity graph store
The entity graph SHALL be persisted in PostgreSQL as the single source of truth. The tables `entities`, `entity_edges`, `entity_observations`, and `cross_tenant_entities` MUST exist in every deployment, regardless of whether SurrealDB or any optional graph store is also enabled. SurrealDB MAY be populated as a best-effort dual-write target, but MUST NOT be the only place where entity data lives, and read APIs MUST NOT depend on it.

#### Scenario: Fresh deployment exposes the entity graph schema
- **WHEN** an operator runs `docker compose down -v && docker compose up -d` on a clean checkout and then connects to PostgreSQL
- **THEN** the four tables `entities`, `entity_edges`, `entity_observations`, and `cross_tenant_entities` exist with the columns and CHECK constraints declared in `init.sql`

#### Scenario: SurrealDB is unavailable but entity graph still works
- **WHEN** the worker pipeline runs an investigation with `ZOVARK_SURREAL_ENABLED=false`
- **THEN** the resulting entities are written to PostgreSQL and `GET /api/v1/entities` returns them

### Requirement: Pipeline writes entities to PostgreSQL
The worker pipeline SHALL write extracted entities, edges, and observations into the PostgreSQL `entities` / `entity_edges` / `entity_observations` tables for every completed investigation. Writes MUST be tenant-scoped (every row carries `tenant_id`), idempotent on `(entity_hash, tenant_id)`, and SHALL set `confidence_source` to one of `clean`, `suspicious`, or `injection_detected` based on the input sanitiser verdict.

#### Scenario: Investigation completion populates entities
- **WHEN** an alert with source IP `185.220.101.45` and username `root` finishes the assess stage with verdict `true_positive`
- **THEN** at least one row appears in `entities` with `entity_type='ip'`, `value='185.220.101.45'`, and the investigation's tenant_id, and at least one row appears in `entity_observations` linking that entity to the investigation

#### Scenario: Re-running the same investigation does not duplicate entities
- **WHEN** the same alert is submitted twice via `force_reinvestigate=true`
- **THEN** the `entities` row count for that `(entity_hash, tenant_id)` stays at one and `observation_count` increments

### Requirement: Entity graph read APIs return populated data
The Go API SHALL expose `GET /api/v1/entities`, `GET /api/v1/entities/:id/neighborhood`, `GET /api/v1/intelligence/top-threats`, and `GET /api/v1/intelligence/stats`, and these endpoints MUST return non-empty results whenever the underlying tables contain rows for the caller's tenant. The endpoints MUST enforce tenant isolation via the `tenant_id` predicate (RLS + WHERE clause).

#### Scenario: Listing entities respects tenant scope
- **WHEN** an analyst calls `GET /api/v1/entities?type=ip` with a JWT for tenant A
- **THEN** the response contains only entities with `tenant_id = A` and the JSON shape `{ "entities": [...], "edges": [...] }`

#### Scenario: Neighborhood traversal returns one-hop edges
- **WHEN** an analyst calls `GET /api/v1/entities/<id>/neighborhood` for an entity that has edges to two other entities in the same tenant
- **THEN** the response contains the seed entity, the two neighbours, and at least the two connecting edges

#### Scenario: Intelligence stats include entity counts
- **WHEN** an analyst calls `GET /api/v1/intelligence/stats`
- **THEN** the response includes a non-zero `total_entities` value computed from `SELECT COUNT(*) FROM entities WHERE tenant_id = $1`

### Requirement: GDPR erasure cascades through entity tables
The `DELETE /api/v1/tenants/:id/data` handler SHALL delete all rows in `entity_observations`, `entity_edges`, and `entities` for the target tenant in FK-safe order, in a single transaction. Foreign keys from `entity_edges.investigation_id` and `entity_observations.investigation_id` to `investigations(id)` MUST exist with `ON DELETE CASCADE` so that deleting an investigation also removes its graph rows.

#### Scenario: Tenant deletion removes all entity data
- **WHEN** an admin calls `DELETE /api/v1/tenants/<tenant_id>/data` for a tenant that has 100 entities, 200 edges, and 500 observations
- **THEN** after the call, `SELECT COUNT(*)` against all three tables filtered by that `tenant_id` returns zero

#### Scenario: Investigation deletion cascades to graph rows
- **WHEN** a row in `investigations` is deleted
- **THEN** all rows in `entity_edges` and `entity_observations` referencing that `investigation_id` are removed by the database, not by application code

### Requirement: Cross-tenant intelligence is computed from base tables
The `refresh_cross_tenant_intel` Temporal activity SHALL recompute `cross_tenant_entities` and `entities.tenant_count` directly from the `entities`, `entity_observations`, and `investigations` tables, without depending on the `cross_tenant_intel` materialized view or `cross_tenant_public` view existing. If the materialized view exists, the activity MAY refresh it for ad-hoc operator queries, but a missing view MUST NOT cause the activity to fail.

#### Scenario: Cross-tenant refresh succeeds when materialized view is absent
- **WHEN** an operator drops `cross_tenant_intel` manually and then runs the `cross_tenant_workflow`
- **THEN** the workflow completes successfully and `cross_tenant_entities` reflects the current `entities` data

#### Scenario: Threat-score recompute updates entities.tenant_count
- **WHEN** an entity hash appears in observations across three different tenants and `refresh_cross_tenant_intel` runs
- **THEN** every `entities` row for that hash has `tenant_count = 3`

### Requirement: STIX/TAXII ingestion matches the entity schema
The `ingest_threat_feed` activity SHALL be gated behind the `ZOVARK_STIX_TAXII_ENABLED` environment variable (default `false`). When enabled, every `INSERT` it performs against `entities` MUST use the canonical column list `(entity_hash, entity_type, value, tenant_id, first_seen, last_seen, threat_score, metadata)`, MUST set `entity_hash = sha256("{entity_type}:{value}").hexdigest()`, MUST use `ON CONFLICT (entity_hash, tenant_id) DO UPDATE`, and MUST only write `entity_type` values that satisfy the table's CHECK constraint (`ip`, `domain`, `file_hash`, `url`, `user`, `device`, `process`, `email`). STIX object types that do not map into this domain MUST be skipped with a counter increment, not coerced.

#### Scenario: STIX indicator with IP pattern is written as an ip entity
- **WHEN** the STIX feed contains an `indicator` object with pattern `[ipv4-addr:value = '203.0.113.5']` and the flag is enabled
- **THEN** one row appears in `entities` with `entity_type='ip'`, `value='203.0.113.5'`, and `entity_hash = sha256('ip:203.0.113.5').hexdigest()`

#### Scenario: STIX malware object is skipped, not coerced
- **WHEN** the STIX feed contains a `malware` object
- **THEN** no row is written to `entities`, an internal counter `stix_skipped_objects{type="malware"}` is incremented, and the activity does not raise

#### Scenario: STIX ingestion is disabled by default
- **WHEN** the worker boots without `ZOVARK_STIX_TAXII_ENABLED` set
- **THEN** the `ingest_threat_feed` activity returns immediately without opening a database connection

### Requirement: Embedding writers do not target dropped columns
No worker code SHALL write to an `embedding` column on `entities`, `entity_edges`, `investigations`, `agent_skills`, `investigation_memory`, `agent_memory_episodic`, `mitre_techniques`, or `investigation_fingerprints`. The `pgvector` extension is retired; any future embedding feature MUST resurrect both the extension and the columns in the same change.

#### Scenario: Worker module compiles with no references to entities.embedding
- **WHEN** a developer runs `grep -rn "entities.*SET embedding" worker/`
- **THEN** zero matches are returned

### Requirement: Migrations and init.sql agree on the entity schema
`init.sql` and the migration chain SHALL declare the same columns, indexes, CHECK constraints, and FK constraints for `entities`, `entity_edges`, `entity_observations`, and `cross_tenant_entities`. A new database created from `init.sql` and an upgraded database created by replaying every migration in order MUST end with byte-equivalent table definitions for the entity-graph objects.

#### Scenario: Schema diff between fresh and upgraded DB is empty for entity tables
- **WHEN** an operator creates one PostgreSQL database from `init.sql` and another by running every migration in `migrations/` in order against an empty DB
- **THEN** `pg_dump --schema-only` of the four entity tables and their indexes is identical between the two databases

### Requirement: End-to-end coverage for the entity graph data path
The test suite SHALL contain at least one Python integration test that submits an alert via the API and asserts the resulting entities are visible through `GET /api/v1/entities`, and at least one Go unit test that exercises `listEntityGraphHandler` and `entityNeighborhoodHandler` against seeded fixture rows. Both MUST run in CI on every pull request.

#### Scenario: Python integration test detects empty read path
- **WHEN** a developer breaks the worker write path so that `entities` is no longer populated
- **THEN** `tests/integration/test_entity_graph_e2e.py` fails on the assertion that the source IP from the seeded alert appears in the entities response

#### Scenario: Go handler test detects shape regression
- **WHEN** a developer renames a column on `entities` without updating `scanEntityRows`
- **THEN** `api/entity_graph_handlers_test.go` fails with a scan error
