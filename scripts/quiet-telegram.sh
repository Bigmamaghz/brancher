#!/usr/bin/env bash
# Force quiet Telegram on Mac Mini: news + trades only, restart daemon.
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

touch .env
# Strip old telegram mode lines, rewrite quiet defaults
grep -vE '^(TELEGRAM_NEWS_ONLY|TELEGRAM_TRADES_ONLY|TELEGRAM_EOD)=' .env > .env.tmp || true
mv .env.tmp .env
cat >> .env <<'EOF'
TELEGRAM_NEWS_ONLY=1
TELEGRAM_TRADES_ONLY=0
TELEGRAM_EOD=0
EOF

git pull --ff-only 2>/dev/null || git pull 2>/dev/null || true
cp -n config/bots.yaml.example config/bots.yaml 2>/dev/null || true

if [[ -x .venv/bin/python3 ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

launchctl unload "$HOME/Library/LaunchAgents/com.bigmamaghz.brancher.plist" 2>/dev/null || true
bash scripts/install_launchagents.sh

echo ""
echo "Quiet mode ON."
echo "  Telegram only for: NEWS (new/changed signals) + ENTER/SELL trades"
echo "  Not sent: status, FAIL, SKIP, ADVANCE timers, EOD"
echo ""
echo "Verify: tail -f data/logs/brancher.log"
