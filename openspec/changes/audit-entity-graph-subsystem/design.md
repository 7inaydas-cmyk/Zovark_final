## Context

The entity graph is a tenant-scoped knowledge graph that stores observed indicators (`entities`), the relationships between them (`entity_edges`), the per-investigation context in which they were seen (`entity_observations`), and a privacy-preserving cross-tenant aggregation (`cross_tenant_entities` + the `cross_tenant_intel` materialized view + the `cross_tenant_public` view that strips tenant IDs). It backs four user-visible surfaces:

1. `GET /api/v1/entities` and `GET /api/v1/entities/:id/neighborhood` (`api/entity_graph_handlers.go`) — the graph view in the dashboard.
2. `GET /api/v1/intelligence/top-threats` and `GET /api/v1/intelligence/stats` (`api/cross_tenant_handlers.go`) — the cross-tenant intel page.
3. `DELETE /api/v1/tenants/:id/data` (`api/gdpr.go`) — GDPR erasure, which deletes from `entity_observations` → `entity_edges` → `entities` in FK order.
4. The `refresh_cross_tenant_intel` Temporal activity (`worker/intelligence/cross_tenant.py`) — periodic refresh + threat-score recompute.

The current state, captured in the audit pass, is split between two incompatible models:

- **Model A (PostgreSQL canonical)** — codified in `init.sql` lines 485–696 and `migrations/001_sprint1g_entity_graph.sql`. Tables exist, indexes exist, the API can read them, GDPR can erase them. This is what every reader assumes.
- **Model B (SurrealDB canonical)** — codified in `migrations/068_ticket2_surreal_graph_pgvector_retirement.sql`. Drops `entities`, `entity_edges`, `entity_observations`, `cross_tenant_intel`, `cross_tenant_public`, all `*.embedding` columns, and the `pgvector` extension. The write path in `worker/entity_graph.write_entity_graph` now delegates to `worker/surreal_graph.write_entity_graph_surreal`, which is gated on `_surreal_enabled()`.

Migration `068` and `init.sql` directly contradict each other on the same objects. Whichever is applied last wins, and there is no test that catches the divergence. Meanwhile, several pieces of worker code reference objects from the in-between state that exists in neither model:

- `worker/intelligence/cross_tenant.py:30` runs `REFRESH MATERIALIZED VIEW CONCURRENTLY cross_tenant_intel` — broken under Model B.
- `worker/intelligence/cross_tenant.py:79–85` runs `SELECT … FROM cross_tenant_public` — broken under Model B.
- `worker/embedding/batch.py:90` and `worker/embedding/versioning.py:157` run `UPDATE entities SET embedding = …::vector` — broken under Model B (column + extension dropped) and arguably broken under Model A too (no production code path produces the embeddings).
- `worker/intelligence/stix_taxii.py:112` runs `INSERT INTO entities (value, type, tenant_id, …) ON CONFLICT (value, type, …)` — broken under **both** models. The columns are `(entity_hash, entity_type, value, …)` and the unique key is `(entity_hash, tenant_id)`. STIX object types (`indicator`, `malware`, `attack-pattern`, …) also do not satisfy the `entity_type` CHECK domain (`ip`, `domain`, `file_hash`, `url`, `user`, `device`, `process`, `email`).

There are no tests that touch any of this end-to-end (no `entity_graph_handlers_test.go`, no `tests/test_entity_graph.py`), so the breakage is invisible until an analyst opens the entity graph page or a GDPR request fires.

## Goals / Non-Goals

**Goals:**
- Pick one canonical store and make every writer, reader, view, and migration agree on it.
- Eliminate every reference in worker / API code to objects that no longer exist in the chosen model.
- Make the entity-graph data path observable: a smoke test that submits one alert, runs the pipeline, and asserts that the resulting entities show up in `GET /api/v1/entities`.
- Ship `docs/AUDIT_ENTITY_GRAPH.md` so the next maintainer does not have to redo this exploration.
- Add the FK constraints from `entity_*` to `investigations` that were missing from migration 001 — required for clean tenant deletes.

**Non-Goals:**
- Resurrecting `pgvector` or any embedding-driven feature. The `*.embedding` columns and HNSW indexes stay dropped; the embedding writers get deleted, not fixed.
- Designing the SurrealDB graph schema in detail. We will keep `worker/surreal_graph.py` as an optional dual-write path but will not make it canonical in this change.
- Rewriting the dashboard graph visualisation. The API contract in `entity_graph_handlers.go` stays stable.
- Implementing a new STIX/TAXII pipeline. We will fix the existing `INSERT` statement (or disable the activity behind a feature flag) but will not redesign the ingester.

## Decisions

### Decision 1: PostgreSQL is the canonical entity graph store

**Choice:** Keep `entities`, `entity_edges`, `entity_observations`, and `cross_tenant_entities` in PostgreSQL. Reverse the destructive parts of migration `068` for the entity-graph tables and views (but not for `pgvector`/embeddings).

**Why:** Every read surface that ships to users (`api/entity_graph_handlers.go`, `api/cross_tenant_handlers.go`, `api/gdpr.go`) already reads from PostgreSQL. The dashboard, the intel page, and GDPR erasure all assume PostgreSQL. SurrealDB is optional infrastructure that not every customer deploys (it is not in `docker-compose.yml` core services). Switching the canonical store to SurrealDB would require rewriting four Go handlers, the GDPR path, the cross-tenant Temporal activity, and adding SurrealDB to the core deployment — a much larger change with worse air-gap-deployment ergonomics.

**Alternatives considered:**
- **SurrealDB canonical** — Rejected. Bigger blast radius, removes a Postgres-only deployment option, and the audit shows zero production-grade reader code that talks to Surreal today.
- **Dual-write, dual-read** — Rejected. The audit shows that this is what we already have on paper and it does not work in practice; there is no consistency check, no failure handling, and the read side picks one store arbitrarily. Pick one, make it work, treat the other as best-effort.

### Decision 2: Reverse migration `068` for entity-graph objects only, in a new migration

**Choice:** Add `migrations/072_entity_graph_restoration.sql` that:
- `CREATE TABLE IF NOT EXISTS` for `entities`, `entity_edges`, `entity_observations` matching `init.sql` (idempotent — if `068` was applied, this restores; if `init.sql` already created them, this is a no-op).
- `CREATE MATERIALIZED VIEW IF NOT EXISTS cross_tenant_intel …` and `CREATE OR REPLACE VIEW cross_tenant_public …` matching `migrations/006_sprint1k_cross_tenant.sql`.
- Adds the missing FK constraints from `entity_edges.investigation_id` and `entity_observations.investigation_id` to `investigations(id)` with `ON DELETE CASCADE`. Use `ALTER TABLE … ADD CONSTRAINT IF NOT EXISTS` (or guarded `DO $$ BEGIN … EXCEPTION WHEN duplicate_object`).
- Does **not** restore `pgvector`, the `embedding` columns, or the HNSW indexes. Those stay retired.

**Why a new migration instead of editing `068`:** We never edit applied migrations. Future-proofing the graph belongs in a new file.

**Why not edit `init.sql`:** `init.sql` is a bootstrap convenience for fresh databases; the migration chain is the source of truth for upgraded ones. Both should converge.

### Decision 3: Rewrite cross-tenant intelligence to compute from base tables, not from views

**Choice:** Rewrite `refresh_cross_tenant_intel` in `worker/intelligence/cross_tenant.py` to:
1. Recompute `cross_tenant_entities` directly from `entities` + `entity_observations` + `investigations` in a single transaction using `INSERT … ON CONFLICT DO UPDATE`.
2. Update `entities.tenant_count` from a CTE on `entity_observations`, no view dependency.
3. `REFRESH MATERIALIZED VIEW CONCURRENTLY cross_tenant_intel` only after step 1, only if the view exists (probe `pg_matviews`).

`get_entity_intelligence` is rewritten to query `cross_tenant_entities` (a real table) instead of `cross_tenant_public`. The view becomes a convenience for ad-hoc SQL only; production code never depends on it.

**Why:** Removes a fragile dependency on a materialized view that has been dropped twice in this codebase's history. Materialized views are an optimisation, not a contract.

### Decision 4: Fix the STIX/TAXII writer to match the real schema, but gate the activity behind a feature flag

**Choice:**
- Rewrite the `INSERT` in `worker/intelligence/stix_taxii.py:112` to use the actual columns: `(entity_hash, entity_type, value, tenant_id, first_seen, last_seen, threat_score, metadata)`.
- Compute `entity_hash = sha256(f"{entity_type}:{value}").hexdigest()` to match the hash convention used elsewhere.
- Map STIX object types into the `entity_type` CHECK domain: `indicator → file_hash|domain|ip|url` (decided by parsing the STIX pattern), `malware/threat-actor/tool/attack-pattern → metadata field` (do not write to `entities` at all). Anything that does not map is dropped with a counter increment.
- Gate the whole `ingest_threat_feed` activity behind `ZOVARK_STIX_TAXII_ENABLED` (default `false`). The audit shows zero production tests for STIX ingest; we do not want to enable a fragile path by default.

**Alternatives considered:** Widen the `entity_type` CHECK to include STIX types. Rejected — pollutes the canonical entity vocabulary, breaks the dashboard, and requires updating every reader.

### Decision 5: Delete the embedding writers, do not gate them

**Choice:** Delete `worker/embedding/batch.py:88–92` and `worker/embedding/versioning.py:157`'s entity embedding writes outright. If a future change wants embeddings back, it can resurrect them with `pgvector` in the same change.

**Why:** Gating dead code is technical debt. Per project conventions in CLAUDE.md, no half-finished implementations.

### Decision 6: Add a real end-to-end test for the entity-graph data path

**Choice:** Add `tests/integration/test_entity_graph_e2e.py` (Python) that:
1. Submits a synthetic alert via the API.
2. Waits for the investigation to complete.
3. Calls `GET /api/v1/entities?type=ip` and asserts the source IP from the alert appears.
4. Calls `GET /api/v1/entities/<id>/neighborhood` and asserts at least one edge.

Plus a Go unit test `api/entity_graph_handlers_test.go` that seeds two rows directly via `db.go`'s pool and asserts the JSON shape.

**Why:** Without coverage, this exact problem will re-occur the next time someone refactors the worker pipeline. The Python test catches end-to-end breakage; the Go test catches handler regressions in isolation.

## Risks / Trade-offs

- **Risk: Migration `072` runs against a DB where `068` was already applied and the tables are gone.** → The `CREATE TABLE IF NOT EXISTS` blocks are idempotent and recreate them empty. There is no historical entity data to recover (it was never populated to Surreal in any deployment we know of). Mitigation: document in the migration header that data loss from `068` is expected and irrecoverable.

- **Risk: Migration `072` runs against a DB where `init.sql` already created the tables (fresh dev install).** → `IF NOT EXISTS` makes it a no-op. Tested on fresh `docker compose down -v && docker compose up -d` before merge.

- **Risk: STIX/TAXII flag stays off forever and the code rots again.** → Mitigated partially by a unit test that asserts the SQL parses against the live schema (no end-to-end test, just a parametrized test that runs the INSERT against a transaction that gets rolled back). We accept the residual risk because STIX is not on the critical path.

- **Risk: SurrealDB users lose the dual-write path.** → `worker/entity_graph.write_entity_graph` keeps calling `write_entity_graph_surreal` after the PostgreSQL write succeeds; failures in the Surreal call are logged but do not abort the transaction. SurrealDB users get the same behaviour they have today, just with PostgreSQL also populated.

- **Trade-off: Reversing `068` re-introduces dead schema for `cross_tenant_intel` and `cross_tenant_public` even though we are rewriting the activity to not depend on them.** → Accepted. The views are cheap, and keeping them lets us run ad-hoc operator SQL the same way it appears in the runbook.

- **Trade-off: Gating STIX ingest behind a flag means it stays untested in CI by default.** → Accepted. We add a smoke test for the SQL only (parses + executes against schema), and we document the flag in CLAUDE.md so a future operator can flip it on deliberately.

## Migration Plan

1. **Land the audit doc first** (`docs/AUDIT_ENTITY_GRAPH.md`) so reviewers can see the evidence behind every decision in this design.
2. **Land migration `072`** in isolation. Verify on a fresh dev DB and on a dev DB where `068` has been applied. Apply via `docker compose exec -T postgres psql -U zovark -d zovark < migrations/072_entity_graph_restoration.sql`.
3. **Land the cross-tenant rewrite** (`worker/intelligence/cross_tenant.py`). Run the existing Temporal `cross_tenant_workflow` against a seeded dev DB and verify `cross_tenant_entities` populates and `entities.tenant_count` updates.
4. **Land the STIX fix** (`worker/intelligence/stix_taxii.py`) with `ZOVARK_STIX_TAXII_ENABLED=false`. Add the parametrized SQL test.
5. **Delete the embedding writers**. Run `python -m py_compile` on the worker tree.
6. **Add the integration test** (`tests/integration/test_entity_graph_e2e.py`) and the Go handler test (`api/entity_graph_handlers_test.go`). Both run in CI.
7. **Update `CLAUDE.md`** Database, Known Issues, and Coding Conventions sections.

**Rollback:** Migration `072` is purely additive (`CREATE … IF NOT EXISTS`, `ADD CONSTRAINT IF NOT EXISTS`). Rolling back means dropping the tables again — but at that point the API regresses to the broken state we started from. Practical rollback is `git revert` of the worker/API changes; the schema additions can stay in place harmlessly.

## Open Questions

- Is there any deployment where `worker/surreal_graph.py` is actually populating SurrealDB with entity data today? If yes, do we need to backfill from Surreal into PostgreSQL during migration `072`? **Default assumption: no.** Confirm with operations before merge.
- Should `cross_tenant_intel` stay a materialized view or become a regular view? Materialized requires manual `REFRESH`; regular re-runs on every query but stays fresh. Pick based on row counts in the largest customer DB. **Default: keep materialized, refresh nightly.**
- Do we need to preserve the `entity_type` CHECK domain or expand it? Current 8 values are fine for the dashboard. STIX gets a separate fix that maps into the existing domain. **Default: do not expand.**
