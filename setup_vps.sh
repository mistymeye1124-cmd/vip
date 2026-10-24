#!/bin/bash
# =============================================================================
# Pro Access Vault VIP Bot - One-Click VPS Installer & Systemd Service Setup
# Supported OS: Ubuntu 20.04 / 22.04 / 24.04, Debian 11 / 12
# =============================================================================

set -e

echo "========================================================"
echo "  🚀 PRO ACCESS VAULT VIP BOT - VPS INSTALLER"
echo "========================================================"
echo ""

# 1. Update system repositories & install system tools
echo "📦 [1/5] Updating packages and installing Python3 stack..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv curl ca-certificates sqlite3 git

# 2. Identify deployment directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "📂 Deploy directory: $SCRIPT_DIR"

# 3. Virtual environment setup
echo "🐍 [2/5] Creating isolated Python virtualenv..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

# 4. Install dependencies
echo "📥 [3/5] Installing Python dependencies..."
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 5. Fix permissions and create runtime folders
echo "📁 [4/5] Setting runtime permissions and directories..."
mkdir -p backups
chmod +x start.sh

# 6. Generate and register dynamic systemd 24/7 service
echo "⚙️ [5/5] Registering 24/7 auto-restart systemd service..."
CURRENT_USER=$(whoami)
SERVICE_NAME="vault_bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

sudo bash -c "cat <<EOF > ${SERVICE_FILE}
[Unit]
Description=Pro Access Vault VIP Bot 24/7 Engine
After=network.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${SCRIPT_DIR}/venv/bin/python bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF"

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}

echo ""
echo "========================================================"
echo "  ✅ VPS INSTALLATION & SYSTEMD SERVICE COMPLETE!"
echo "========================================================"
echo ""
echo "To START the bot in background now:"
echo "   sudo systemctl start ${SERVICE_NAME}"
echo ""
echo "To CHECK status:"
echo "   sudo systemctl status ${SERVICE_NAME}"
echo ""
echo "To VIEW LIVE LOGS:"
echo "   sudo journalctl -u ${SERVICE_NAME} -f"
echo ""
echo "To STOP or RESTART:"
echo "   sudo systemctl stop ${SERVICE_NAME}"
echo "   sudo systemctl restart ${SERVICE_NAME}"
echo ""
echo "Or run directly in terminal:"
echo "   ./start.sh"
echo "========================================================"
