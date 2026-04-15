#!/bin/bash
# ============================================================
# ZOVARK Database Backup Script
# Dumps PostgreSQL, compresses, uploads to MinIO
# Retention: 7 daily + 4 weekly backups
# Usage: ./scripts/backup-db.sh
# ============================================================
# Audit 3.18: bash (not sh) so PIPESTATUS is available; strict pipefail; no
# more silent password default; stderr redirected so pg_dump failures surface.
set -euo pipefail

# Required: POSTGRES_PASSWORD must be set via env / secrets — no dev default.
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set (no silent default)}"

# Configuration
POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-zovark}"
POSTGRES_DB="${POSTGRES_DB:-zovark}"
MINIO_ALIAS="${MINIO_ALIAS:-zovark}"
MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}"
MINIO_BUCKET="${MINIO_BUCKET:-zovark-backups}"
MINIO_ACCESS_KEY="${MINIO_ROOT_USER:-zovark}"
MINIO_SECRET_KEY="${MINIO_ROOT_PASSWORD:-zovark_dev_2026}"
BACKUP_DIR="${BACKUP_DIR:-/tmp/zovark-backups}"
DAILY_RETENTION=7
WEEKLY_RETENTION=4
BACKUP_PASSPHRASE="${BACKUP_PASSPHRASE:-}"

# Timestamp
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DAY_OF_WEEK=$(date +%u)
DAILY_FILE="zovark_daily_${TIMESTAMP}.sql.gz"
WEEKLY_FILE="zovark_weekly_${TIMESTAMP}.sql.gz"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Create backup directory
mkdir -p "${BACKUP_DIR}"

# Configure MinIO client (mc)
if command -v mc >/dev/null 2>&1; then
    mc alias set "${MINIO_ALIAS}" "${MINIO_ENDPOINT}" "${MINIO_ACCESS_KEY}" "${MINIO_SECRET_KEY}" 2>/dev/null || true
    mc mb --ignore-existing "${MINIO_ALIAS}/${MINIO_BUCKET}" 2>/dev/null || true
else
    log "WARNING: mc (MinIO client) not found — backup will be stored locally only"
fi

# ─── DUMP DATABASE ──────────────────────────────────────
log "Starting PostgreSQL backup of '${POSTGRES_DB}'..."

export PGPASSWORD="${POSTGRES_PASSWORD}"
BACKUP_PATH="${BACKUP_DIR}/${DAILY_FILE}"

pg_dump \
    -h "${POSTGRES_HOST}" \
    -p "${POSTGRES_PORT}" \
    -U "${POSTGRES_USER}" \
    -d "${POSTGRES_DB}" \
    --format=plain \
    --no-owner \
    --no-acl \
    --verbose 2>"${BACKUP_DIR}/${DAILY_FILE}.stderr" | gzip > "${BACKUP_PATH}"

# Audit 3.18: pipefail isn't sufficient when only the tail command succeeds —
# check PIPESTATUS explicitly so a failed pg_dump can't produce a clean gzip.
if [ "${PIPESTATUS[0]}" -ne 0 ]; then
    log "ERROR: pg_dump failed (see ${BACKUP_DIR}/${DAILY_FILE}.stderr)"
    cat "${BACKUP_DIR}/${DAILY_FILE}.stderr" >&2 || true
    rm -f "${BACKUP_PATH}"
    exit 1
fi

BACKUP_SIZE=$(ls -lh "${BACKUP_PATH}" | awk '{print $5}')
log "Backup created: ${BACKUP_PATH} (${BACKUP_SIZE})"

# ─── ENCRYPT BACKUP (Security P2#29) ─────────────────
if [ -n "${BACKUP_PASSPHRASE}" ] && command -v gpg >/dev/null 2>&1; then
    log "Encrypting backup with AES-256..."
    gpg --batch --yes --passphrase "${BACKUP_PASSPHRASE}" \
        --symmetric --cipher-algo AES256 \
        --output "${BACKUP_PATH}.gpg" "${BACKUP_PATH}"
    rm -f "${BACKUP_PATH}"
    BACKUP_PATH="${BACKUP_PATH}.gpg"
    DAILY_FILE="${DAILY_FILE}.gpg"
    WEEKLY_FILE="${WEEKLY_FILE}.gpg"
    BACKUP_SIZE=$(ls -lh "${BACKUP_PATH}" | awk '{print $5}')
    log "Encrypted backup: ${BACKUP_PATH} (${BACKUP_SIZE})"
elif [ -z "${BACKUP_PASSPHRASE}" ]; then
    log "WARNING: BACKUP_PASSPHRASE not set — backup stored UNENCRYPTED"
fi

# ─── UPLOAD TO MINIO ───────────────────────────────────
if command -v mc >/dev/null 2>&1; then
    # Upload daily backup
    log "Uploading daily backup to MinIO..."
    mc cp "${BACKUP_PATH}" "${MINIO_ALIAS}/${MINIO_BUCKET}/daily/${DAILY_FILE}"

    # On Sunday (day 7), also save a weekly backup
    if [ "${DAY_OF_WEEK}" = "7" ]; then
        log "Sunday — creating weekly backup..."
        mc cp "${BACKUP_PATH}" "${MINIO_ALIAS}/${MINIO_BUCKET}/weekly/${WEEKLY_FILE}"
    fi

    # ─── RETENTION CLEANUP ──────────────────────────────
    log "Enforcing retention policy..."

    # Clean daily backups (keep last N)
    DAILY_COUNT=$(mc ls "${MINIO_ALIAS}/${MINIO_BUCKET}/daily/" 2>/dev/null | wc -l)
    if [ "${DAILY_COUNT}" -gt "${DAILY_RETENTION}" ]; then
        DELETE_COUNT=$((DAILY_COUNT - DAILY_RETENTION))
        log "Removing ${DELETE_COUNT} old daily backups..."
        mc ls "${MINIO_ALIAS}/${MINIO_BUCKET}/daily/" 2>/dev/null | head -n "${DELETE_COUNT}" | awk '{print $NF}' | while read -r file; do
            mc rm "${MINIO_ALIAS}/${MINIO_BUCKET}/daily/${file}" 2>/dev/null || true
        done
    fi

    # Clean weekly backups (keep last N)
    WEEKLY_COUNT=$(mc ls "${MINIO_ALIAS}/${MINIO_BUCKET}/weekly/" 2>/dev/null | wc -l)
    if [ "${WEEKLY_COUNT}" -gt "${WEEKLY_RETENTION}" ]; then
        DELETE_COUNT=$((WEEKLY_COUNT - WEEKLY_RETENTION))
        log "Removing ${DELETE_COUNT} old weekly backups..."
        mc ls "${MINIO_ALIAS}/${MINIO_BUCKET}/weekly/" 2>/dev/null | head -n "${DELETE_COUNT}" | awk '{print $NF}' | while read -r file; do
            mc rm "${MINIO_ALIAS}/${MINIO_BUCKET}/weekly/${file}" 2>/dev/null || true
        done
    fi

    log "Retention enforced: ${DAILY_RETENTION} daily, ${WEEKLY_RETENTION} weekly"
fi

# Clean local temp file
rm -f "${BACKUP_PATH}"

log "Backup complete: ${DAILY_FILE}"
