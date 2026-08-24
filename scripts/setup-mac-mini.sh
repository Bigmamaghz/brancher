#!/usr/bin/env bash
# One-shot Mac Mini setup: install deps, test Telegram, start Brancher as daemon.
set -euo pipefail

REPO_CANDIDATES=(
  "$(cd "$(dirname "$0")/.." && pwd)"
  "$HOME/brancher"
  "$HOME/resulter trader"
  "/Users/josephghouzi/resulter trader"
  "/Users/josephghouzi/brancher"
)

REPO_ROOT=""
for d in "${REPO_CANDIDATES[@]}"; do
  if [[ -f "$d/pyproject.toml" && -f "$d/src/cli.py" ]]; then
    REPO_ROOT="$d"
    break
  fi
done

if [[ -z "$REPO_ROOT" ]]; then
  REPO_ROOT="$HOME/brancher"
  echo "Cloning Brancher to $REPO_ROOT ..."
  git clone https://github.com/Bigmamaghz/brancher.git "$REPO_ROOT"
fi

cd "$REPO_ROOT"
echo "Using repo: $REPO_ROOT"

git pull --ff-only 2>/dev/null || git pull 2>/dev/null || true

if ! command -v python3 >/dev/null; then
  echo "python3 not found. Install: brew install python3"
  exit 1
fi

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]" -q

mkdir -p data/logs config
cp -n config/bots.yaml.example config/bots.yaml 2>/dev/null || true

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo ""
  echo "Created .env — paste your Alpaca paper + Telegram keys, then run this script again."
  open -e .env 2>/dev/null || true
  exit 1
fi

echo ""
echo "Testing Telegram ..."
python -m src.cli ping

echo ""
echo "Installing LaunchAgent (runs at login, keeps alive) ..."
bash scripts/install_launchagents.sh

echo ""
echo "============================================"
echo " Brancher is LIVE on this Mac Mini"
echo "============================================"
echo "  Logs:  tail -f $REPO_ROOT/data/logs/brancher.log"
echo "  Stop:  launchctl stop com.bigmamaghz.brancher"
echo "  Start: launchctl start com.bigmamaghz.brancher"
echo "  Status: python -m src.cli status"
echo "============================================"
