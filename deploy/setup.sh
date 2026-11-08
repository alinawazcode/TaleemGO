#!/usr/bin/env bash
# ==============================================================================
# TaleemGo AI - Automated Setup Script for Azure Ubuntu 24.04 LTS (x86_64)
# Run once as root or with sudo: sudo bash deploy/setup.sh
# Idempotent: safe to run multiple times.
#
# Generates BRIDGE_TOKEN and TALEEMGO_SECRET_KEY automatically.
# Never prints secrets to the console.
# ==============================================================================

set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "Error: This setup script must be run with root privileges (sudo)." >&2
  exit 1
fi

echo "======================================================================"
echo "          TaleemGo AI - Azure Ubuntu 24.04 Production Setup           "
echo "======================================================================"

# --- 1. System packages & dependencies ---
echo "[1/9] Updating system packages and installing base dependencies..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  git python3 python3-venv python3-pip build-essential curl ca-certificates \
  unzip ufw wget gnupg openssl qrencode

echo "Installing headless Chrome dependencies for Ubuntu 24.04..."
apt-get install -y --no-install-recommends \
  fonts-liberation libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 \
  libcairo2 libcups2t64 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 \
  libnspr4 libnss3 libpango-1.0-0 libx11-6 libx11-xcb1 libxcb1 \
  libxcomposite1 libxdamage1 libxext6 libxfixes3 libxrandr2 libxshmfence1 \
  xdg-utils

# --- 2. Node.js LTS via NodeSource ---
echo "[2/9] Checking Node.js installation..."
NEED_NODE=true
if command -v node >/dev/null 2>&1; then
  NODE_VER="$(node -v | cut -d'.' -f1 | tr -d 'v')"
  if [ "${NODE_VER}" -ge 20 ]; then
    NEED_NODE=false
    echo "  Node.js v${NODE_VER} already installed."
  fi
fi

if [ "${NEED_NODE}" = true ]; then
  echo "  Installing Node.js 22 LTS from official NodeSource repository..."
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
  echo "  Installed Node.js $(node -v) and npm $(npm -v)."
fi

# --- 3. Google Chrome Stable (.deb) ---
echo "[3/9] Checking Google Chrome Stable..."
if [ ! -x "/usr/bin/google-chrome-stable" ]; then
  echo "  Downloading and installing official Google Chrome stable deb..."
  TMP_DEB="$(mktemp /tmp/chrome-XXXXXX.deb)"
  wget -q -O "${TMP_DEB}" "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
  apt-get install -y "${TMP_DEB}"
  rm -f "${TMP_DEB}"
fi
echo "  Google Chrome: $(google-chrome-stable --version)"

# --- 4. 2 GB Swap File & Swappiness ---
echo "[4/9] Configuring 2 GB swap file and kernel swappiness..."
if [ ! -f /swapfile ]; then
  echo "  Creating /swapfile (2 GB)..."
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  if ! grep -q '/swapfile' /etc/fstab; then
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
else
  echo "  /swapfile already exists."
  if ! swapon --show | grep -q '/swapfile'; then
    swapon /swapfile || true
  fi
fi

# Set swappiness to 10
echo "vm.swappiness=10" > /etc/sysctl.d/99-taleemgo-swap.conf
sysctl -p /etc/sysctl.d/99-taleemgo-swap.conf >/dev/null 2>&1 || sysctl vm.swappiness=10 >/dev/null 2>&1 || true

# --- 5. User & Directory Structure ---
echo "[5/9] Setting up service user and directories..."
if ! id -u taleemgo >/dev/null 2>&1; then
  useradd -r -s /bin/bash -d /opt/taleemgo -m taleemgo
  echo "  Created system user 'taleemgo'."
else
  echo "  User 'taleemgo' already exists."
fi

mkdir -p /opt/taleemgo/backups
mkdir -p /opt/taleemgo/.ssh
chmod 700 /opt/taleemgo/.ssh

APP_SRC="/opt/taleemgo/app-src"
DEPLOY_KEY="/opt/taleemgo/.ssh/deploy_key"
SSH_CMD="ssh -i ${DEPLOY_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

if [ -f "${DEPLOY_KEY}" ]; then
  chmod 600 "${DEPLOY_KEY}"
  chown taleemgo:taleemgo "${DEPLOY_KEY}"
fi

# Clone or synchronize repository into /opt/taleemgo/app-src
if [ ! -d "${APP_SRC}/.git" ]; then
  # Detect if setup.sh was launched from an existing checkout
  CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  if [ -d "${CURRENT_DIR}/.git" ] && [ "${CURRENT_DIR}" != "${APP_SRC}" ]; then
    echo "  Syncing repository from ${CURRENT_DIR} to ${APP_SRC}..."
    mkdir -p "${APP_SRC}"
    cp -a "${CURRENT_DIR}/." "${APP_SRC}/"
  else
    echo ""
    echo "--- Private Repository Setup ---"
    if [ -f "${DEPLOY_KEY}" ]; then
      echo "Using deploy key at ${DEPLOY_KEY}."
    else
      echo "Notice: Deploy key not found at ${DEPLOY_KEY}."
      echo "If cloning fails, generate one with: ssh-keygen -t ed25519 -f ${DEPLOY_KEY}"
      echo "and add the public key under GitHub repo > Settings > Deploy keys."
    fi
    read -rp "Enter private repository Git URL (e.g. git@github.com:owner/repo.git): " REPO_INPUT
    if [ -f "${DEPLOY_KEY}" ]; then
      GIT_SSH_COMMAND="${SSH_CMD}" git clone "${REPO_INPUT}" "${APP_SRC}"
    else
      git clone "${REPO_INPUT}" "${APP_SRC}"
    fi
  fi
else
  echo "  Repository already present at ${APP_SRC}. Updating..."
  cd "${APP_SRC}"
  if [ -f "${DEPLOY_KEY}" ]; then
    sudo -u taleemgo GIT_SSH_COMMAND="${SSH_CMD}" git pull --ff-only || true
  else
    sudo -u taleemgo git pull --ff-only || true
  fi
fi

# Ensure correct branch if needed
cd "${APP_SRC}"

# --- 6. Python Virtualenv & Node Dependencies ---
echo "[6/9] Installing application dependencies..."
VENV_DIR="/opt/taleemgo/venv"
if [ ! -d "${VENV_DIR}" ]; then
  echo "  Creating Python virtualenv at ${VENV_DIR}..."
  python3 -m venv "${VENV_DIR}"
fi
echo "  Installing Python requirements..."
"${VENV_DIR}/bin/pip" install --upgrade pip -q
"${VENV_DIR}/bin/pip" install -q -r "${APP_SRC}/requirements.txt"

echo "  Installing WhatsApp bridge Node.js dependencies..."
cd "${APP_SRC}/whatsapp_bridge"
export PUPPETEER_SKIP_DOWNLOAD=1
if [ -f "package-lock.json" ]; then
  npm ci --omit=dev || npm install --omit=dev
else
  npm install --omit=dev
fi

# Check postinstall patch marker
PATCH_FILE="${APP_SRC}/whatsapp_bridge/node_modules/whatsapp-web.js/src/util/Injected/Utils.js"
if [ -f "${PATCH_FILE}" ] && grep -q "/\*saathi-patch\*/" "${PATCH_FILE}"; then
  echo "  whatsapp-web.js patch verified."
else
  echo "  Applying whatsapp-web.js patch..."
  node "${APP_SRC}/whatsapp_bridge/patch-wwebjs.js"
  if ! grep -q "/\*saathi-patch\*/" "${PATCH_FILE}"; then
    echo "Error: whatsapp-web.js patch failed verification!" >&2
    exit 1
  fi
fi

# --- 7. Configure /opt/taleemgo/.env ---
echo "[7/9] Configuring environment variables (/opt/taleemgo/.env)..."
PROD_ENV="/opt/taleemgo/.env"
EXAMPLE_ENV="${APP_SRC}/deploy/env.production.example"

if [ ! -f "${PROD_ENV}" ]; then
  echo "  Creating /opt/taleemgo/.env from production template..."
  cp "${EXAMPLE_ENV}" "${PROD_ENV}"

  # Auto-generate secure tokens
  GEN_BRIDGE_TOKEN="$(openssl rand -hex 32)"
  GEN_FERNET_KEY="$("${VENV_DIR}/bin/python" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

  # Prompt interactively for environment inputs
  echo ""
  echo "--- Interactive Configuration ---"
  echo "(Press ENTER to skip optional fields)"
  read -rp "Admin WhatsApp Phone Number (e.g. 923001234567, or Enter to skip): " IN_ADMIN_NUM
  read -s -rp "Google Gemini API Key (hidden, or Enter to skip): " IN_GEMINI_KEY
  echo ""
  read -s -rp "Groq API Key (hidden, or Enter to skip): " IN_GROQ_KEY
  echo ""

  # Populate .env file safely
  sed -i "s|^BRIDGE_TOKEN=.*|BRIDGE_TOKEN=${GEN_BRIDGE_TOKEN}|" "${PROD_ENV}"
  sed -i "s|^TALEEMGO_SECRET_KEY=.*|TALEEMGO_SECRET_KEY=${GEN_FERNET_KEY}|" "${PROD_ENV}"
  sed -i "s|^CHROME_PATH=.*|CHROME_PATH=/usr/bin/google-chrome-stable|" "${PROD_ENV}"
  sed -i "s|^MONITOR_INTERVAL_MINUTES=.*|MONITOR_INTERVAL_MINUTES=30|" "${PROD_ENV}"
  sed -i "s|^MAX_USERS=.*|MAX_USERS=5|" "${PROD_ENV}"
  sed -i "s|^LLM_PROVIDERS=.*|LLM_PROVIDERS=gemini,groq|" "${PROD_ENV}"
  sed -i "s|^GEMINI_MODELS=.*|GEMINI_MODELS=gemini-3.5-flash-lite,gemini-3.8-flash,gemini-3.6-flash|" "${PROD_ENV}"
  sed -i "s|^GROQ_MODELS=.*|GROQ_MODELS=openai/gpt-oss-20b,openai/gpt-oss-120b|" "${PROD_ENV}"

  if [ -n "${IN_ADMIN_NUM}" ]; then
    CLEAN_ADMIN="$(echo "${IN_ADMIN_NUM}" | tr -cd '0-9')"
    sed -i "s|^ADMIN_NUMBER=.*|ADMIN_NUMBER=${CLEAN_ADMIN}|" "${PROD_ENV}"
  fi
  if [ -n "${IN_GEMINI_KEY}" ]; then
    sed -i "s|^GEMINI_API_KEY=.*|GEMINI_API_KEY=${IN_GEMINI_KEY}|" "${PROD_ENV}"
  fi
  if [ -n "${IN_GROQ_KEY}" ]; then
    sed -i "s|^GROQ_API_KEY=.*|GROQ_API_KEY=${IN_GROQ_KEY}|" "${PROD_ENV}"
  fi

  echo "  Environment configuration written with auto-generated secrets."
  echo "  Note: Review SUPABASE_URL, SUPABASE_ANON_KEY, and LMS_BASE_URL in ${PROD_ENV}."
else
  echo "  /opt/taleemgo/.env already exists; preserving existing configuration."
fi

chmod 600 "${PROD_ENV}"
chown taleemgo:taleemgo "${PROD_ENV}"
chown -R taleemgo:taleemgo /opt/taleemgo

# --- 8. Install Systemd Services and Timer ---
echo "[8/9] Installing systemd service units..."
cp "${APP_SRC}/deploy/taleemgo-api.service" /etc/systemd/system/taleemgo-api.service
cp "${APP_SRC}/deploy/taleemgo-bridge.service" /etc/systemd/system/taleemgo-bridge.service
cp "${APP_SRC}/deploy/taleemgo-backup.service" /etc/systemd/system/taleemgo-backup.service
cp "${APP_SRC}/deploy/taleemgo-backup.timer" /etc/systemd/system/taleemgo-backup.timer

systemctl daemon-reload

echo "  Enabling and starting taleemgo-api service..."
systemctl enable --now taleemgo-api.service

echo "  Enabling and starting taleemgo-backup timer..."
systemctl enable --now taleemgo-backup.timer

echo "  Note: taleemgo-bridge is NOT enabled automatically."
echo "  You must perform the initial WhatsApp QR scan interactively (see instructions below)."

# --- 9. Firewall (UFW) ---
echo "[9/9] Configuring firewall (UFW)..."
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable
echo "  Firewall enabled: OpenSSH allowed, all other incoming ports blocked."

echo ""
echo "======================================================================"
echo "                   SETUP COMPLETED SUCCESSFULLY!                      "
echo "======================================================================"
echo "Next step: Link WhatsApp by scanning the QR code in your terminal:"
echo ""
echo "1. Run the bridge in the foreground to scan the QR code:"
echo "   sudo -u taleemgo -H bash -c 'cd /opt/taleemgo/app-src/whatsapp_bridge && set -a && . /opt/taleemgo/.env && set +a && node bridge.js'"
echo ""
echo "2. Scan the QR code displayed on screen (WhatsApp > Linked devices)."
echo "3. Once it logs 'WhatsApp ready', press Ctrl+C."
echo "4. Enable and start the background bridge service:"
echo "   sudo systemctl enable --now taleemgo-bridge"
echo ""
echo "5. Check status anytime:"
echo "   bash /opt/taleemgo/app-src/deploy/status.sh"
echo "======================================================================"
