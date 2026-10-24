#!/bin/bash
# =============================================================================
# Pro Access Vault VIP Bot - Foreground Terminal Runner
# =============================================================================
cd "$(dirname "$0")"

if [ -f "./venv/bin/python" ]; then
    ./venv/bin/python bot.py
else
    python3 bot.py
fi
