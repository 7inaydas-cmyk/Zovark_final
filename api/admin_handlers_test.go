package main

import (
	"net/http"
	"testing"
)

// TestProbeDBHandler_Forbidden verifies that the analyst role cannot reach
// POST /api/v1/admin/diagnostics/probe-db. The endpoint must be admin-only
// because it triggers a write transaction against a global diagnostic table.
func TestProbeDBHandler_Forbidden(t *testing.T) {
	router := setupTestRouter()
	token := createTestJWT("tenant-1", "user-1", "analyst@zovark.local", "analyst")
	w := makeRequest(router, "POST", "/api/v1/admin/diagnostics/probe-db", nil, token)
	if w.Code != http.StatusForbidden {
		t.Errorf("Analyst should get 403 on probe-db, got %d body=%s", w.Code, w.Body.String())
	}
}

// TestProbeDBHandler_AdminReachesHandler verifies that an admin token gets
// past requireRole("admin") and into probeDBHandler. The hermetic test router
// has no live dbPool, so the handler returns 500 from dbPool.Begin — but a
// 500 still proves RBAC let the admin through (a 403 would mean the route
// is wired wrong). The full success path (200 + ok:true + row_id) is covered
// by the §7 e2e probe Stage 0.5 against the live stack.
func TestProbeDBHandler_AdminReachesHandler(t *testing.T) {
	router := setupTestRouter()
	token := createTestJWT("tenant-1", "user-1", "admin@zovark.local", "admin")
	w := makeRequest(router, "POST", "/api/v1/admin/diagnostics/probe-db", nil, token)
	if w.Code == http.StatusForbidden {
		t.Errorf("Admin should not get 403 on probe-db, got 403")
	}
}

// TestParseQueryExecMode_Defaults verifies the env var helper produces the
// PgBouncer-safe default for empty input and rejects unknown values with a
// loud error. This is the lower half of the §1 tasks — the upper half (pool
// build + self-test) cannot be unit-tested without a live pgxpool.
func TestParseQueryExecMode_Defaults(t *testing.T) {
	cases := []struct {
		in        string
		wantLabel string
		wantErr   bool
	}{
		{"", "describe_exec", false},
		{"exec", "exec", false},
		{"describe_exec", "describe_exec", false},
		{"simple_protocol", "simple_protocol", false},
		{"cache_statement", "cache_statement", false},
		{"foobar", "", true},
		{"EXEC", "", true},
	}
	for _, c := range cases {
		_, label, err := parseQueryExecMode(c.in)
		if c.wantErr {
			if err == nil {
				t.Errorf("parseQueryExecMode(%q) expected error, got nil", c.in)
				continue
			}
			if !contains(err.Error(), "unknown ZOVARK_PGX_QUERY_MODE value") {
				t.Errorf("parseQueryExecMode(%q) error %q missing canonical phrase", c.in, err.Error())
			}
			continue
		}
		if err != nil {
			t.Errorf("parseQueryExecMode(%q) unexpected error: %v", c.in, err)
			continue
		}
		if label != c.wantLabel {
			t.Errorf("parseQueryExecMode(%q) label = %q, want %q", c.in, label, c.wantLabel)
		}
	}
}

// TestRequireSchemaLedger_EnvParsing verifies the env-var helper accepts the
// expected truthy values and treats everything else (including unset) as off.
// The ledger drift check defaults to non-blocking so dev workflows with fresh
// volumes don't get gated on a manual migration run.
func TestRequireSchemaLedger_EnvParsing(t *testing.T) {
	cases := []struct {
		val  string
		want bool
	}{
		{"", false},
		{"true", true},
		{"TRUE", true},
		{"True", true},
		{"1", true},
		{"yes", true},
		{"YES", true},
		{"false", false},
		{"0", false},
		{"no", false},
		{"foobar", false},
	}
	for _, c := range cases {
		t.Setenv("ZOVARK_REQUIRE_SCHEMA_LEDGER", c.val)
		got := requireSchemaLedger()
		if got != c.want {
			t.Errorf("requireSchemaLedger() with env=%q = %v, want %v", c.val, got, c.want)
		}
	}
	// Unset case
	t.Setenv("ZOVARK_REQUIRE_SCHEMA_LEDGER", "")
	if requireSchemaLedger() {
		t.Error("requireSchemaLedger() with empty env should be false")
	}
}

func contains(haystack, needle string) bool {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return true
		}
	}
	return false
}
