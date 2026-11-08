#!/usr/bin/env bash
# ==============================================================================
# TaleemGo AI - System and Service Status Check
# Reports service states, system memory, bridge health, disk space, and LLM logs.
# Never echoes secrets or raw tokens.
# ==============================================================================

set -euo pipefail

echo "======================================================================"
echo "                   TaleemGo AI Status Dashboard                       "
echo "======================================================================"
echo "Timestamp: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo ""

echo "--- 1. Systemd Service State ---"
API_STATE="$(systemctl is-active taleemgo-api 2>/dev/null || echo 'not-found')"
BRIDGE_STATE="$(systemctl is-active taleemgo-bridge 2>/dev/null || echo 'not-found')"
TIMER_STATE="$(systemctl is-active taleemgo-backup.timer 2>/dev/null || echo 'not-found')"

echo "  taleemgo-api:    ${API_STATE}"
echo "  taleemgo-bridge: ${BRIDGE_STATE}"
echo "  backup.timer:    ${TIMER_STATE}"
echo ""

echo "--- 2. System & Process Memory ---"
free -h
echo ""
echo "Service Memory Footprint:"
API_MEM="$(systemctl show -p MemoryCurrent taleemgo-api 2>/dev/null | cut -d'=' -f2 || echo 'N/A')"
BRIDGE_MEM="$(systemctl show -p MemoryCurrent taleemgo-bridge 2>/dev/null | cut -d'=' -f2 || echo 'N/A')"

format_bytes() {
  local b="${1:-}"
  if [[ "$b" =~ ^[0-9]+$ ]]; then
    if [ "$b" -gt 1048576 ]; then
      echo "$(( b / 1048576 )) MB"
    else
      echo "$(( b / 1024 )) KB"
    fi
  else
    echo "Inactive / N/A"
  fi
}

echo "  taleemgo-api memory:    $(format_bytes "${API_MEM}")"
echo "  taleemgo-bridge memory: $(format_bytes "${BRIDGE_MEM}") (MemoryMax=2800M)"
echo ""

echo "--- 3. Disk Space ---"
df -h / | awk 'NR==1 || NR==2 {print "  "$0}'
echo ""

echo "--- 4. WhatsApp Bridge Health (/health) ---"
ENV_FILE="/opt/taleemgo/.env"
if [ -f "${ENV_FILE}" ]; then
  # Read BRIDGE_TOKEN safely without echoing it to terminal or logs
  BRIDGE_TOKEN="$(grep -E '^BRIDGE_TOKEN=' "${ENV_FILE}" | cut -d'=' -f2- | tr -d '"'"'" | tr -d '[:space:]')"
  HEALTH_RESP="$(curl -s -m 5 -H "x-token: ${BRIDGE_TOKEN}" http://127.0.0.1:3001/health 2>/dev/null || echo 'HTTP_CONNECT_FAILED')"
  if [ "${HEALTH_RESP}" = "HTTP_CONNECT_FAILED" ]; then
    echo "  Bridge endpoint (127.0.0.1:3001) not responding or offline."
  else
    echo "  Response: ${HEALTH_RESP}"
  fi
else
  echo "  Notice: ${ENV_FILE} not found."
fi
echo ""

echo "--- 5. Recent [LLM] Diagnostic Logs (last 10) ---"
if command -v journalctl >/dev/null 2>&1; then
  LLM_LOGS="$(journalctl -u taleemgo-api --no-pager -n 300 2>/dev/null | grep '\[LLM\]' | tail -n 10 || true)"
  if [ -n "${LLM_LOGS}" ]; then
    echo "${LLM_LOGS}"
  else
    echo "  (No [LLM] log entries recorded yet)"
  fi
else
  echo "  journalctl not available."
fi
echo "======================================================================"

