package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"time"

	"github.com/exaring/otelpgx"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

var dbPool *pgxpool.Pool

// parseQueryExecMode maps the ZOVARK_PGX_QUERY_MODE env value to a pgx exec mode.
// Empty string defaults to "describe_exec" — the PgBouncer-transaction-pool-safe
// mode that ALSO does a Describe round-trip so pgx can infer parameter OIDs for
// values whose Go type doesn't pin down a Postgres type (notably
// map[string]interface{} → jsonb). Plain "exec" is faster (no Describe) but
// cannot encode maps / structs into jsonb columns, so it is offered as an
// override only. "cache_statement" is the dangerous pgx default that triggers
// SQLSTATE 08P01 against PgBouncer transaction pooling; offered for direct-
// postgres test stacks. Unknown values return an error so a typo is loud at
// boot instead of silently falling back to a dangerous default.
func parseQueryExecMode(envValue string) (pgx.QueryExecMode, string, error) {
	v := envValue
	if v == "" {
		v = "describe_exec"
	}
	switch v {
	case "exec":
		return pgx.QueryExecModeExec, "exec", nil
	case "describe_exec":
		return pgx.QueryExecModeDescribeExec, "describe_exec", nil
	case "simple_protocol":
		return pgx.QueryExecModeSimpleProtocol, "simple_protocol", nil
	case "cache_statement":
		return pgx.QueryExecModeCacheStatement, "cache_statement", nil
	default:
		return 0, "", fmt.Errorf("unknown ZOVARK_PGX_QUERY_MODE value %q (expected exec, describe_exec, simple_protocol, cache_statement)", envValue)
	}
}

func initDB(dbURL string) error {
	cfg, err := pgxpool.ParseConfig(dbURL)
	if err != nil {
		return fmt.Errorf("parse database url: %w", err)
	}
	mode, modeString, err := parseQueryExecMode(os.Getenv("ZOVARK_PGX_QUERY_MODE"))
	if err != nil {
		return err
	}
	cfg.ConnConfig.DefaultQueryExecMode = mode
	if otelTracerProvider != nil {
		opts := []otelpgx.Option{otelpgx.WithTracerProvider(otelTracerProvider)}
		if otelMeterProvider != nil {
			opts = append(opts, otelpgx.WithMeterProvider(otelMeterProvider))
		}
		cfg.ConnConfig.Tracer = otelpgx.NewTracer(opts...)
	}
	dbPool, err = pgxpool.NewWithConfig(context.Background(), cfg)
	if err != nil {
		return err
	}
	slog.Info("pgx_pool_query_mode", "mode", modeString)
	if err := dbPool.Ping(context.Background()); err != nil {
		return err
	}
	selfTestCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := selfTestPool(selfTestCtx, dbPool); err != nil {
		slog.Error("pgx_pool_self_test_failed", "err", err.Error())
		return err
	}
	slog.Info("pgx_pool_self_test", "result", "passed")

	driftCtx, driftCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer driftCancel()
	n, err := checkSchemaMigrationsLedger(driftCtx, dbPool)
	if err != nil {
		slog.Error("schema_migrations_check", "status", "query_error", "err", err.Error())
		if requireSchemaLedger() {
			return fmt.Errorf("schema_migrations check failed and ZOVARK_REQUIRE_SCHEMA_LEDGER=true: %w", err)
		}
	} else if n == 0 {
		msg := "schema_migrations ledger absent or empty — run scripts/apply_migrations.sh; see docs/RUNBOOK_HEALTHCHECK.md#schema-drift"
		if requireSchemaLedger() {
			slog.Error("schema_migrations_check", "status", "absent", "hint", msg)
			return fmt.Errorf("%s", msg)
		}
		slog.Warn("schema_migrations_check", "status", "absent", "hint", msg)
	} else {
		slog.Info("schema_migrations_check", "status", "present", "applied", n)
	}
	return nil
}

// checkSchemaMigrationsLedger returns the row count of the schema_migrations
// ledger or (0, nil) if the table does not exist (SQLSTATE 42P01). Any other
// error is returned to the caller for the strict-env decision path.
func checkSchemaMigrationsLedger(ctx context.Context, pool *pgxpool.Pool) (int, error) {
	var n int
	err := pool.QueryRow(ctx, "SELECT count(*) FROM schema_migrations").Scan(&n)
	if err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "42P01" {
			return 0, nil
		}
		return 0, err
	}
	return n, nil
}

// requireSchemaLedger reads ZOVARK_REQUIRE_SCHEMA_LEDGER and returns true for
// "true" / "TRUE" / "1" / "yes" (case-insensitive). Anything else, including
// unset, returns false. When true, the API refuses to start against a DB that
// has no ledger or whose ledger query errors. Default off so dev workflows
// (`docker compose down -v && up -d`) don't require a manual migration step.
func requireSchemaLedger() bool {
	v := os.Getenv("ZOVARK_REQUIRE_SCHEMA_LEDGER")
	switch v {
	case "true", "TRUE", "True", "1", "yes", "YES":
		return true
	default:
		return false
	}
}

// selfTestPool catches a regression where the pgx pool was built in a query
// exec mode that uses named prepared statements while DATABASE_URL points at
// PgBouncer in transaction pooling mode. The collision surfaces as Postgres
// SQLSTATE 08P01 ("prepared statement … already exists"). We force it on
// startup by acquiring two distinct backend connections from the pool and
// running a parameterised round-trip on each. If the mode is unsafe, the
// second round-trip raises 08P01 and we fail-fast with an error pointing
// the operator at the env override and the runbook.
func selfTestPool(ctx context.Context, pool *pgxpool.Pool) error {
	c1, err := pool.Acquire(ctx)
	if err != nil {
		return fmt.Errorf("pgx self-test: acquire conn 1: %w", err)
	}
	if err := c1.Conn().QueryRow(ctx, "SELECT $1::int", 1).Scan(new(int)); err != nil {
		c1.Release()
		return wrapSelfTestErr(err)
	}
	c1.Release()

	c2, err := pool.Acquire(ctx)
	if err != nil {
		return fmt.Errorf("pgx self-test: acquire conn 2: %w", err)
	}
	defer c2.Release()
	if err := c2.Conn().QueryRow(ctx, "SELECT $1::int", 2).Scan(new(int)); err != nil {
		return wrapSelfTestErr(err)
	}
	return nil
}

func wrapSelfTestErr(err error) error {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "08P01" {
		return fmt.Errorf(
			"pgx pool query mode is incompatible with PgBouncer transaction pooling "+
				"(SQLSTATE 08P01 prepared statement collision). "+
				"Set ZOVARK_PGX_QUERY_MODE=describe_exec (or exec, or use a direct postgres connection without PgBouncer for tests). "+
				"See docs/RUNBOOK_HEALTHCHECK.md#api-08p01. underlying: %w",
			err,
		)
	}
	return fmt.Errorf("pgx self-test: %w", err)
}

func closeDB() {
	if dbPool != nil {
		dbPool.Close()
	}
}

// validateTenantID ensures a tenant_id string is a parseable UUID before it is
// interpolated into SET LOCAL app.current_tenant. Every callsite that opens a
// tenant transaction MUST go through this helper — otherwise a bug elsewhere
// that lets user input reach tenant_id becomes SQL injection on every
// subsequent statement.
func validateTenantID(tenantID string) error {
	if _, err := uuid.Parse(tenantID); err != nil {
		return fmt.Errorf("invalid tenant id: %w", err)
	}
	return nil
}

// detachedCleanupCtx returns a new context that is NOT tied to the request
// context, with a short deadline. Use for failure-path writes (status=failed,
// audit events on error) where the request context may already be cancelled.
func detachedCleanupCtx() (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), 5*time.Second)
}

// isRelationMissing returns true if err is Postgres 42P01 (undefined_table) or
// 42703 (undefined_column). Used by handlers that want to tolerate a fresh DB
// where a view/column hasn't been migrated yet, WITHOUT also swallowing real
// failures like connection pool exhaustion or permission-denied.
func isRelationMissing(err error) bool {
	if err == nil {
		return false
	}
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code == "42P01" || pgErr.Code == "42703"
	}
	return false
}

// beginTenantTx starts a transaction with RLS tenant context set.
// The caller MUST call tx.Commit() or tx.Rollback() when done.
func beginTenantTx(ctx context.Context, tenantID string) (pgx.Tx, error) {
	if err := validateTenantID(tenantID); err != nil {
		return nil, err
	}
	tx, err := dbPool.Begin(ctx)
	if err != nil {
		return nil, fmt.Errorf("begin tenant tx: %w", err)
	}
	// SET LOCAL cannot use $1 parameters through PgBouncer transaction pooling,
	// so we interpolate. Safe because validateTenantID() guaranteed tenantID is
	// a well-formed UUID containing only [0-9a-f-].
	_, err = tx.Exec(ctx, fmt.Sprintf("SET LOCAL app.current_tenant = '%s'", tenantID))
	if err != nil {
		tx.Rollback(ctx)
		return nil, fmt.Errorf("set tenant context: %w", err)
	}
	return tx, nil
}
