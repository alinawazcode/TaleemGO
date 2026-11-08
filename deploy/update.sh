#!/usr/bin/env bash
# ==============================================================================
# TaleemGo AI - Service Update Script
# Updates repository, refreshes dependencies, restarts services, and verifies logs.
# Never prints secrets or token values.
# ==============================================================================

set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "Error: Please run as root or with sudo: sudo bash deploy/update.sh" >&2
  exit 1
fi

APP_DIR="/opt/taleemgo/app-src"
VENV_PIP="/opt/taleemgo/venv/bin/pip"
DEPLOY_KEY="/opt/taleemgo/.ssh/deploy_key"

echo "======================================================================"
echo "                   TaleemGo AI Software Update                       "
echo "======================================================================"

cd "${APP_DIR}"

echo "[1/4] Pulling latest changes from Git (fast-forward only)..."
if [ -f "${DEPLOY_KEY}" ]; then
  sudo -u taleemgo GIT_SSH_COMMAND="ssh -i ${DEPLOY_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" git pull --ff-only
else
  sudo -u taleemgo git pull --ff-only
fi

echo "[2/4] Verifying and updating dependencies..."
if [ -f "${VENV_PIP}" ]; then
  echo "Checking Python packages..."
  "${VENV_PIP}" install -q -r "${APP_DIR}/requirements.txt"
fi

cd "${APP_DIR}/whatsapp_bridge"
echo "Checking Node.js dependencies..."
export PUPPETEER_SKIP_DOWNLOAD=1
if [ -f "package-lock.json" ]; then
  sudo -u taleemgo -E npm ci --omit=dev || sudo -u taleemgo -E npm install --omit=dev
else
  sudo -u taleemgo -E npm install --omit=dev
fi

# Ensure whatsapp-web.js patch is applied
PATCH_FILE="${APP_DIR}/whatsapp_bridge/node_modules/whatsapp-web.js/src/util/Injected/Utils.js"
if ! grep -q "/\*saathi-patch\*/" "${PATCH_FILE}" 2>/dev/null; then
  echo "Applying whatsapp-web.js media patch..."
  sudo -u taleemgo node "${APP_DIR}/whatsapp_bridge/patch-wwebjs.js"
fi

echo "[3/4] Restarting systemd services..."
systemctl restart taleemgo-api

if systemctl is-enabled --quiet taleemgo-bridge 2>/dev/null || systemctl is-active --quiet taleemgo-bridge 2>/dev/null; then
  systemctl restart taleemgo-bridge
else
  echo "Note: taleemgo-bridge is not yet enabled (run initial QR scan before enabling)."
fi

echo "[4/4] Verifying service states..."
echo "  taleemgo-api:    $(systemctl is-active taleemgo-api 2>/dev/null || echo 'inactive')"
echo "  taleemgo-bridge: $(systemctl is-active taleemgo-bridge 2>/dev/null || echo 'inactive')"
echo ""

echo "--- Recent Logs (taleemgo-api) ---"
journalctl -u taleemgo-api -n 20 --no-pager
echo ""

if systemctl is-active --quiet taleemgo-bridge 2>/dev/null; then
  echo "--- Recent Logs (taleemgo-bridge) ---"
  journalctl -u taleemgo-bridge -n 20 --no-pager
  echo ""
fi

echo "======================================================================"
echo "Update complete."

