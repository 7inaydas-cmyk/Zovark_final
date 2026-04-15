## Why

The entity graph subsystem is in a half-migrated state: migration `068_ticket2_surreal_graph_pgvector_retirement.sql` drops the PostgreSQL `entities` / `entity_edges` / `entity_observations` tables, the `cross_tenant_intel` materialized view, the `cross_tenant_public` view, and the `pgvector` extension — but `init.sql` recreates the tables, worker writers still try to refresh dropped views and write to dropped `embedding` columns, the STIX/TAXII ingester writes to columns and conflict targets that do not exist, and Go API handlers (`/api/v1/entities`, `/api/v1/entities/:id/neighborhood`, `/api/v1/intelligence/*`) read from PostgreSQL tables that the SurrealDB write path no longer populates. The result is several latent crashes and a fully decoupled read/write surface that silently returns empty data.

## What Changes

- **Audit deliverable**: produce `docs/AUDIT_ENTITY_GRAPH.md` mapping every table / view / index / migration / writer / reader, with the inconsistencies catalogued and severity-ranked.
- **Pick a single source of truth**: decide whether the entity graph lives in PostgreSQL (re-enable dual-write from `entity_graph.write_entity_graph`) or SurrealDB (rewrite API handlers + intelligence activities to read from Surreal). Default recommendation: keep PostgreSQL as the canonical store and treat Surreal as optional, since the read API and GDPR erase path already depend on it.
- **Fix `worker/intelligence/stix_taxii.py`**: align the INSERT column list and `ON CONFLICT` target with the real `entities` schema (`entity_hash`, `entity_type`, `value`, `tenant_id`, …), and map STIX object types into the `entity_type` CHECK domain (or widen the CHECK).
- **Fix `worker/intelligence/cross_tenant.py`**: either restore `cross_tenant_intel` / `cross_tenant_public` (add a migration that recreates them) or rewrite `refresh_cross_tenant_intel` and `get_entity_intelligence` to compute the same result directly from `entities` + `entity_observations`.
- **Fix `worker/embedding/batch.py` and `worker/embedding/versioning.py`**: stop writing to the dropped `entities.embedding` column. Either gate the code on a feature flag that matches `pgvector` availability, or remove it.
- **Reconcile `init.sql` with migration `068`**: pick one. Either roll back the destructive parts of `068` (recreate the views, drop the column drops) or remove the entity tables from `init.sql` and rewrite the API to be Surreal-backed. The audit doc will recommend; this change will execute the recommendation.
- **Add FK constraints** from `entity_edges.investigation_id` and `entity_observations.investigation_id` back to `investigations(id)` so GDPR erasure and tenant deletes cascade cleanly.
- **Add minimal coverage**: one Go handler test for `listEntityGraphHandler` against a seeded fixture, and one Python test that runs `extract_entities` → `write_entity_graph` → API read end-to-end so the broken path cannot silently return empty again.
- **BREAKING** (only if we pick the Surreal-canonical route): `/api/v1/entities*` and `/api/v1/intelligence/*` response shapes may shift; documented in design.md.

## Capabilities

### New Capabilities
- `entity-graph`: defines the contract for the entity graph store — what tables/collections exist, what writers populate them, what API endpoints read from them, and the consistency model between Postgres and SurrealDB. This is the spec the audit and the fixes both ratify against.

### Modified Capabilities
<!-- None — no prior specs exist in openspec/specs/. -->

## Impact

- **Code**: `worker/entity_graph.py`, `worker/surreal_graph.py`, `worker/intelligence/cross_tenant.py`, `worker/intelligence/stix_taxii.py`, `worker/embedding/batch.py`, `worker/embedding/versioning.py`, `api/entity_graph_handlers.go`, `api/cross_tenant_handlers.go`, `api/gdpr.go`.
- **Schema**: new migration recreating the cross-tenant views (or replacing them with a function), adding FK constraints on `entity_edges.investigation_id` and `entity_observations.investigation_id`, and reconciling `init.sql` against migration `068`.
- **APIs**: `GET /api/v1/entities`, `GET /api/v1/entities/:id/neighborhood`, `GET /api/v1/intelligence/top-threats`, `GET /api/v1/intelligence/stats` start returning real data again.
- **Operational**: GDPR erase (`DELETE /api/v1/tenants/:id/data`), STIX/TAXII feed ingest, and the `refresh_cross_tenant_intel` Temporal activity stop crashing.
- **Tests**: one new Go handler test, one new Python integration test, plus the audit findings doc under `docs/`.
- **Docs**: `docs/AUDIT_ENTITY_GRAPH.md` (new), `CLAUDE.md` "Database" + "Known Issues" sections updated to reflect the resolved state.
