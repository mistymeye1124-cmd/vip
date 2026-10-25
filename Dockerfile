FROM python:3.12-slim

# Prevent Python from writing .pyc files and enable real-time unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install system dependencies:
# - ca-certificates: Secure SSL connections to Telegram API
# - curl: Network diagnostics & HTTP checks
# - tzdata: Accurate timezone conversion (Asia/Dhaka)
# - sqlite3: Database inspection, maintenance & integrity checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    tzdata \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip and install Python requirements first (layer-cached)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# Copy application source code (filtered by .dockerignore)
COPY . .

# Ensure backup directory exists
RUN mkdir -p backups

# Graceful shutdown signal for asyncio & python-telegram-bot
STOPSIGNAL SIGTERM

# Container healthcheck ensuring Telegram API connectivity
HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('https://api.telegram.org', timeout=5)" || exit 1

CMD ["python", "bot.py"]
