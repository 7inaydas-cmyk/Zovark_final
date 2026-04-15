package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// MigrationRunner handles database schema migrations using SQL files.
// Tracks applied migrations in a schema_migrations table.
// Uses PostgreSQL advisory locks to prevent concurrent runs.
func runMigrations(command string, args []string) {
	if dbPool == nil {
		log.Fatal("Database not initialized")
	}

	switch command {
	case "up":
		migrateUp()
	case "version":
		migrateVersion()
	case "status":
		migrateStatus()
	default:
		fmt.Printf("Unknown migration command: %s\n", command)
		fmt.Println("Usage: migrate [up|version|status]")
	}
}

func ensureMigrationTable() {
	// Schema matches migrations/072_schema_migrations_ledger.sql so this code
	// path and the bash runner (scripts/apply_migrations.sh) write to the
	// same ledger and produce comparable rows. See
	// docs/RUNBOOK_HEALTHCHECK.md#schema-drift for the design notes.
	_, err := dbPool.Exec(context.Background(), `
		CREATE TABLE IF NOT EXISTS schema_migrations (
			filename     TEXT PRIMARY KEY,
			applied_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
			applied_by   TEXT NOT NULL DEFAULT current_user,
			source       TEXT NOT NULL CHECK (source IN ('init_sql', 'migration_runner', 'manual_backfill')),
			checksum     TEXT
		)
	`)
	if err != nil {
		log.Fatalf("Failed to create schema_migrations table: %v", err)
	}
}

func acquireMigrationLock() bool {
	var acquired bool
	err := dbPool.QueryRow(context.Background(),
		"SELECT pg_try_advisory_lock(42)",
	).Scan(&acquired)
	if err != nil {
		log.Printf("Warning: could not acquire advisory lock: %v", err)
		return false
	}
	return acquired
}

func releaseMigrationLock() {
	dbPool.Exec(context.Background(), "SELECT pg_advisory_unlock(42)")
}

func getAppliedMigrations() map[string]bool {
	ensureMigrationTable()
	rows, err := dbPool.Query(context.Background(),
		"SELECT filename FROM schema_migrations ORDER BY filename")
	if err != nil {
		log.Fatalf("Failed to query migrations: %v", err)
	}
	defer rows.Close()

	applied := make(map[string]bool)
	for rows.Next() {
		var filename string
		rows.Scan(&filename)
		applied[filename] = true
	}
	return applied
}

func getMigrationFiles() []string {
	// Look for migrations in ./migrations/ relative to working directory
	dirs := []string{"./migrations", "../migrations", "/app/migrations"}
	var migrationDir string
	for _, d := range dirs {
		if info, err := os.Stat(d); err == nil && info.IsDir() {
			migrationDir = d
			break
		}
	}
	if migrationDir == "" {
		log.Println("No migrations directory found")
		return nil
	}

	entries, err := os.ReadDir(migrationDir)
	if err != nil {
		log.Fatalf("Failed to read migrations directory: %v", err)
	}

	var files []string
	for _, entry := range entries {
		if !entry.IsDir() && strings.HasSuffix(entry.Name(), ".sql") {
			files = append(files, filepath.Join(migrationDir, entry.Name()))
		}
	}
	sort.Strings(files)
	return files
}

func migrateUp() {
	if !acquireMigrationLock() {
		log.Fatal("Another migration is in progress (advisory lock held)")
	}
	defer releaseMigrationLock()

	applied := getAppliedMigrations()
	files := getMigrationFiles()

	pending := 0
	for _, file := range files {
		version := filepath.Base(file)
		if applied[version] {
			continue
		}

		log.Printf("Applying migration: %s", version)
		content, err := os.ReadFile(file)
		if err != nil {
			log.Fatalf("Failed to read %s: %v", file, err)
		}

		_, err = dbPool.Exec(context.Background(), string(content))
		if err != nil {
			log.Fatalf("Migration %s failed: %v", version, err)
		}

		_, err = dbPool.Exec(context.Background(),
			"INSERT INTO schema_migrations (filename, applied_by, source, checksum) VALUES ($1, $2, 'migration_runner', '') ON CONFLICT (filename) DO NOTHING",
			version, "hydra-api migrate up")
		if err != nil {
			log.Fatalf("Failed to record migration %s: %v", version, err)
		}

		log.Printf("Applied: %s", version)
		pending++
	}

	if pending == 0 {
		log.Println("No pending migrations")
	} else {
		log.Printf("Applied %d migrations", pending)
	}
}

func migrateVersion() {
	applied := getAppliedMigrations()
	var latest string
	for v := range applied {
		if v > latest {
			latest = v
		}
	}
	if latest == "" {
		fmt.Println("No migrations applied")
	} else {
		fmt.Printf("Current version: %s (%d total applied)\n", latest, len(applied))
	}
}

func migrateStatus() {
	applied := getAppliedMigrations()
	files := getMigrationFiles()

	fmt.Printf("%-45s %s\n", "MIGRATION", "STATUS")
	fmt.Println(strings.Repeat("-", 60))
	for _, file := range files {
		version := filepath.Base(file)
		status := "PENDING"
		if applied[version] {
			status = "APPLIED"
		}
		fmt.Printf("%-45s %s\n", version, status)
	}
	fmt.Printf("\nTotal: %d applied, %d pending\n",
		len(applied), len(files)-len(applied))
}
