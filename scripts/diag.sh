#!/usr/bin/env bash
# Diagnose "анкета не падает в админку".
# Run on the same host where taxi-bot.service is running.
#
# Usage:
#   ./scripts/diag.sh
#
# Prints: env file, DB path/size, webhook info, recent driver_registration logs.

set -e
cd "$(dirname "$0")/.."

echo "=== .env (BASE_URL, DATABASE_URL, ADMIN_TELEGRAM_IDS, MINI_APP_URL) ==="
if [[ -f .env ]]; then
  grep -E "^(BASE_URL|DATABASE_URL|ADMIN_TELEGRAM_IDS|MINI_APP_URL|BOT_TOKEN)=" .env \
    | sed 's/\(BOT_TOKEN=\).*/\1***hidden***/'
else
  echo ".env not found"
fi

echo
echo "=== DB files ==="
ls -la taxi_bot.db* 2>/dev/null || echo "no taxi_bot.db* files"

echo
echo "=== /healthz ==="
PORT="${TAXI_PORT:-8000}"
HOST="${TAXI_HOST:-127.0.0.1}"
curl -s "http://${HOST}:${PORT}/healthz" || echo "(healthz unreachable)"
echo

echo
echo "=== getWebhookInfo ==="
if [[ -f .env ]]; then
  BOT_TOKEN=$(grep -E "^BOT_TOKEN=" .env | head -1 | sed 's/^BOT_TOKEN=//;s/^"//;s/"$//')
  if [[ -n "$BOT_TOKEN" ]]; then
    curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo" \
      | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d.get('result', d), indent=2, ensure_ascii=False))" \
      || echo "(failed to parse webhook info)"
  else
    echo "BOT_TOKEN not set in .env"
  fi
fi

echo
echo "=== Recent journal (driver_registration / Webhook / Driver persisted) ==="
if command -v journalctl >/dev/null 2>&1; then
  sudo journalctl -u taxi-bot -n 300 --no-pager 2>/dev/null \
    | grep -E "Webhook update|Driver .* persisted|driver_registration|create_paired_proposals|Startup:" \
    | tail -n 50 \
    || echo "(no matching lines)"
else
  echo "journalctl not available"
fi
