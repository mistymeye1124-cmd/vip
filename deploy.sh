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
    echo "[+] Flushing database WAL to disk via container..."
    docker exec pro_access_vault_bot python3 -c "from database import db; db.checkpoint()" || true
    echo "[+] Running online atomic backup via container..."
    docker exec pro_access_vault_bot python3 backup_dispatcher.py "PRE-UPDATE SAFETY BACKUP" || echo "[-] Container pre-backup script returned non-zero, continuing with local host snapshot."
else
    echo "[!] Container is not currently running. Running host backup if python3 is available..."
    python3 backup_dispatcher.py "PRE-UPDATE SAFETY BACKUP" || true
fi

# Physical host backup snapshot
mkdir -p backups
if [ -d "vault_database.db" ] && [ -f "vault_database.db/vault.db" ]; then
    cp -v "vault_database.db/vault.db" "backups/host_pre_update_$(date +%Y%m%d_%H%M%S).db"
elif [ -f "vault_database.db" ]; then
    cp -v "vault_database.db" "backups/host_pre_update_$(date +%Y%m%d_%H%M%S).db"
else
    touch vault_database.db
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
