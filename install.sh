#!/usr/bin/env bash
#
# One-click installer for the Biometric Attendance Bridge (local server).
#
# Sets up a Python virtualenv, installs dependencies, writes the configuration,
# and (optionally) installs a systemd service that keeps the bridge running and
# restarts it on boot/crash.
#
# Usage (interactive):
#     sudo bash install.sh
#
# Usage (non-interactive, e.g. for provisioning):
#     sudo NONINTERACTIVE=1 ODOO_URL=https://acme.odoo.com TRANSPORT=json2 \
#          API_KEY=xxxxx AUTO_DISCOVER=true INTERVAL=5 bash install.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="biometric-bridge"

# ----- Defaults (override via environment) -------------------------------
INSTALL_DIR="${INSTALL_DIR:-/opt/${APP_NAME}}"
ODOO_URL="${ODOO_URL:-}"
ODOO_DB="${ODOO_DB:-}"                    # required only for multi-DB hosts
TRANSPORT="${TRANSPORT:-json2}"          # json2 | custom
API_KEY="${API_KEY:-}"
AUTO_DISCOVER="${AUTO_DISCOVER:-true}"   # true | false
INTERVAL="${INTERVAL:-5}"                # minutes
SINCE_HOURS="${SINCE_HOURS:-24}"
DEVICE_CODE="${DEVICE_CODE:-}"           # only for single-device mode
DEVICE_HOST="${DEVICE_HOST:-}"           # only for single-device mode
DEVICE_PORT="${DEVICE_PORT:-4370}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
NONINTERACTIVE="${NONINTERACTIVE:-0}"
INSTALL_SERVICE="${INSTALL_SERVICE:-1}"

c_green() { printf '\033[0;32m%s\033[0m\n' "$1"; }
c_yellow() { printf '\033[0;33m%s\033[0m\n' "$1"; }
c_red() { printf '\033[0;31m%s\033[0m\n' "$1"; }

ask() {
    # ask "Prompt" "default" -> echoes answer
    local prompt="$1" default="${2:-}" reply
    if [ "$NONINTERACTIVE" = "1" ]; then
        echo "$default"; return
    fi
    if [ -n "$default" ]; then
        read -r -p "$prompt [$default]: " reply || true
        echo "${reply:-$default}"
    else
        read -r -p "$prompt: " reply || true
        echo "$reply"
    fi
}

echo "============================================================"
c_green " Biometric Attendance Bridge - Installer"
echo "============================================================"

# ----- Gather settings ---------------------------------------------------
if [ "$NONINTERACTIVE" != "1" ]; then
    INSTALL_DIR="$(ask 'Install directory' "$INSTALL_DIR")"
    ODOO_URL="$(ask 'Odoo URL (https://company.odoo.com)' "$ODOO_URL")"
    ODOO_DB="$(ask 'Odoo database name (blank if single-DB host)' "$ODOO_DB")"
    TRANSPORT="$(ask 'Transport (json2/custom)' "$TRANSPORT")"
    if [ "$TRANSPORT" = "json2" ]; then
        echo "  -> Provide a USER API key (Odoo: Preferences > Account Security > New API Key)"
    else
        echo "  -> Provide a device API key (Odoo: Biometric > Devices > device form)"
    fi
    API_KEY="$(ask 'API key' "$API_KEY")"
    AUTO_DISCOVER="$(ask 'Auto-discover all devices from Odoo? (true/false)' "$AUTO_DISCOVER")"
    if [ "$AUTO_DISCOVER" != "true" ]; then
        DEVICE_CODE="$(ask 'Device code (single-device mode)' "$DEVICE_CODE")"
        DEVICE_HOST="$(ask 'Device IP (single-device mode)' "$DEVICE_HOST")"
        DEVICE_PORT="$(ask 'Device port' "$DEVICE_PORT")"
    fi
    INTERVAL="$(ask 'Sync interval (minutes)' "$INTERVAL")"
    SERVICE_USER="$(ask 'Run service as user' "$SERVICE_USER")"
fi

if [ -z "$ODOO_URL" ] || [ -z "$API_KEY" ]; then
    c_red "ERROR: Odoo URL and API key are required."
    exit 1
fi

# ----- Locate python -----------------------------------------------------
PYTHON_BIN="$(command -v python3 || true)"
if [ -z "$PYTHON_BIN" ]; then
    c_red "ERROR: python3 not found. Install Python 3.8+ first."
    exit 1
fi

# ----- Create install dir + venv ----------------------------------------
c_yellow "Installing to: $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/biometric_middleware.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/" 2>/dev/null || true

c_yellow "Creating virtual environment..."
"$PYTHON_BIN" -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip >/dev/null
c_yellow "Installing dependencies (pyzk, requests)..."
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" >/dev/null \
    || "$INSTALL_DIR/venv/bin/pip" install pyzk requests >/dev/null

# ----- Write config.ini --------------------------------------------------
CONFIG_FILE="$INSTALL_DIR/config.ini"
c_yellow "Writing configuration: $CONFIG_FILE"
cat > "$CONFIG_FILE" <<EOF
[odoo]
url = ${ODOO_URL}
db = ${ODOO_DB}
transport = ${TRANSPORT}
api_key = ${API_KEY}
auto_discover = ${AUTO_DISCOVER}
device_code = ${DEVICE_CODE}
timeout = 30

[device]
host = ${DEVICE_HOST}
port = ${DEVICE_PORT}
password = 0
type = auto

[sync]
interval_minutes = ${INTERVAL}
batch_size = 100
clear_after_sync = false
since_hours = ${SINCE_HOURS}
retry_attempts = 3
retry_delay_seconds = 10

[logging]
level = INFO
file =
EOF
chmod 600 "$CONFIG_FILE"

# ----- Optional test run -------------------------------------------------
RUN_TEST="$(ask 'Run a test sync now? (yes/no)' 'no')"
if [ "$RUN_TEST" = "yes" ]; then
    c_yellow "Running a single sync cycle..."
    "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/biometric_middleware.py" \
        --config "$CONFIG_FILE" --once -v || c_red "Test sync reported errors (see above)."
fi

# ----- systemd service ---------------------------------------------------
if [ "$INSTALL_SERVICE" = "1" ] && command -v systemctl >/dev/null 2>&1; then
    SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
    c_yellow "Installing systemd service: $SERVICE_FILE"
    SUDO=""
    [ "$(id -u)" -ne 0 ] && SUDO="sudo"
    $SUDO tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=Biometric Attendance Bridge (Odoo)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/biometric_middleware.py --config ${CONFIG_FILE} --daemon
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable "${APP_NAME}.service"
    $SUDO systemctl restart "${APP_NAME}.service"
    c_green "Service installed and started."
    echo
    echo "Manage it with:"
    echo "  systemctl status ${APP_NAME}"
    echo "  journalctl -u ${APP_NAME} -f"
    echo "  systemctl restart ${APP_NAME}"
else
    c_yellow "Skipping systemd service (not available or disabled)."
    echo "Run manually with:"
    echo "  ${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/biometric_middleware.py --config ${CONFIG_FILE} --daemon"
fi

echo
c_green "Done. Configuration: ${CONFIG_FILE}"
