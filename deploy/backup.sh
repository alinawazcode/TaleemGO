#!/usr/bin/env bash
# ==============================================================================
# TaleemGo AI - Daily Backup Script
# Creates a compressed archive of .env, user data, and WhatsApp session.
#
# CAUTION: Backups contain confidential credentials (API keys, session tokens,
# database records). NEVER push backups to git, store them on public clouds,
# or send them over unencrypted channels/email.
# ==============================================================================

set -euo pipefail

BACKUP_DIR="/opt/taleemgo/backups"
APP_DIR="/opt/taleemgo/app-src"
ENV_FILE="/opt/taleemgo/.env"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
ARCHIVE_NAME="taleemgo-backup-${TIMESTAMP}.tar.gz"
TARGET_ARCHIVE="${BACKUP_DIR}/${ARCHIVE_NAME}"

mkdir -p "${BACKUP_DIR}"

# Build list of existing items to include
ITEMS=()
if [ -f "${ENV_FILE}" ]; then
  ITEMS+=("-C" "/opt/taleemgo" ".env")
fi
if [ -d "${APP_DIR}/data" ]; then
  ITEMS+=("-C" "${APP_DIR}" "data")
fi
if [ -d "${APP_DIR}/whatsapp_bridge/.wwebjs_auth" ]; then
  ITEMS+=("-C" "${APP_DIR}/whatsapp_bridge" ".wwebjs_auth")
fi

if [ ${#ITEMS[@]} -eq 0 ]; then
  echo "[BACKUP] Warning: No target files found to back up."
  exit 0
fi

echo "[BACKUP] Creating archive: ${TARGET_ARCHIVE}"
tar -czf "${TARGET_ARCHIVE}" "${ITEMS[@]}"
chmod 600 "${TARGET_ARCHIVE}"
if id -u taleemgo >/dev/null 2>&1; then
  chown taleemgo:taleemgo "${TARGET_ARCHIVE}"
fi

ARCHIVE_SIZE="$(du -h "${TARGET_ARCHIVE}" | cut -f1)"
echo "[BACKUP] Completed successfully (${ARCHIVE_SIZE})."

# Prune old archives, retaining the most recent 7
echo "[BACKUP] Rotating archives (keeping last 7)..."
find "${BACKUP_DIR}" -maxdepth 1 -name "taleemgo-backup-*.tar.gz" -type f | \
  sort -r | \
  tail -n +8 | \
  xargs -r rm -f

BACKUP_COUNT="$(find "${BACKUP_DIR}" -maxdepth 1 -name "taleemgo-backup-*.tar.gz" -type f | wc -l)"
echo "[BACKUP] Retained ${BACKUP_COUNT} backup archives in ${BACKUP_DIR}."
