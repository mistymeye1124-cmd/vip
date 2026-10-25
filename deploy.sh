#!/usr/bin/env bash
# ==============================================================================
# Pro Access Vault — Bulletproof Auto-Backup & Zero-Downtime Deployment Script
# ==============================================================================
set -e

echo "=========================================================="
echo "🛡️  STEP 1: Automated Pre-Update Telegram Backup Dispatch"
echo "=========================================================="

# Check if container is running to trigger online atomic backup
if docker ps --format '{{.Names}}' | grep -q "pro_access_vault_bot"; then
    echo "[+] Running online atomic backup from active container..."
    docker exec pro_access_vault_bot python3 -c "
import asyncio, os, json, datetime
from database import db
from telegram import Bot

try:
    with open('config.json') as f:
        cfg = json.load(f)
    token = os.getenv('BOT_TOKEN') or cfg.get('bot_token')
    admins = cfg.get('admin_ids', [8387415921])
    backup_file = db.backup_database()
    now_str = datetime.datetime.now().strftime('%d %b %Y, %I:%M:%S %p')
    bot = Bot(token)
    for aid in admins:
        try:
            with open(backup_file, 'rb') as bf:
                asyncio.run(bot.send_document(
                    chat_id=aid,
                    document=bf,
                    filename=os.path.basename(backup_file),
                    caption=(
                        '🛡️ <b>[PRE-UPDATE SAFETY BACKUP DISPATCHED]</b>\n'
                        '━━━━━━━━━━━━━━━━━━━━\n'
                        f'📅 <b>Time:</b> <code>{now_str}</code>\n'
                        '⚠️ <b>Event:</b> Automatic safety snapshot dispatched BEFORE deploying code update.\n'
                        '✅ <i>Your database is 100% secured and protected against any accident!</i>'
                    ),
                    parse_mode='HTML'
                ))
            print(f'✅ Dispatched pre-update backup to Admin {aid}')
        except Exception as e:
            print(f'⚠️ Failed to send to {aid}: {e}')
except Exception as ge:
    print(f'⚠️ Pre-update backup dispatch error: {ge}')
" || echo "[-] Container pre-backup script encountered a non-fatal warning."
else
    echo "[!] Container is not currently running. Safe to proceed."
fi

# Physical host backup snapshot
mkdir -p backups
if [ -d "vault_database.db" ] && [ -f "vault_database.db/vault.db" ]; then
    cp -v "vault_database.db/vault.db" "backups/host_pre_update_$(date +%Y%m%d_%H%M%S).db"
elif [ -f "vault_database.db" ]; then
    cp -v "vault_database.db" "backups/host_pre_update_$(date +%Y%m%d_%H%M%S).db"
fi

echo "=========================================================="
echo "📥 STEP 2: Pulling Latest Code from Git Repository"
echo "=========================================================="
git pull origin main

echo "=========================================================="
echo "🚀 STEP 3: Building & Restarting Docker Container"
echo "=========================================================="
docker compose up -d --build

echo "=========================================================="
echo "✅ DEPLOYMENT FINISHED — Checking Container Health"
echo "=========================================================="
sleep 3
docker ps | grep pro_access_vault_bot
echo "=========================================================="
docker logs --tail 25 pro_access_vault_bot
