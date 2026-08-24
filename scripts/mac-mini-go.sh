#!/usr/bin/env bash
# Exact Mac Mini commands — copy/paste this whole block into Terminal.
set -euo pipefail

cd "$HOME/brancher" 2>/dev/null || cd "/Users/josephmini/brancher" 2>/dev/null || {
  echo "Repo not found. Clone first:"
  echo "  git clone https://github.com/Bigmamaghz/brancher.git ~/brancher"
  exit 1
}

echo "Repo: $(pwd)"
git pull

python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
bash scripts/install-deps.sh

# Keep your filled .env; only create if missing
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created blank .env — fill Alpaca + Telegram + Author keys, then re-run."
  open -e .env
  exit 1
fi

# Sync bot registry
cp config/bots.yaml.example config/bots.yaml

echo ""
echo "=== Telegram ping ==="
python3 -m src.cli ping

echo ""
echo "=== Poll Authors + send UPDATE digests to Telegram ==="
python3 -m src.cli update

echo ""
echo "=== Install LaunchAgent (keeps Brancher alive every 15 min) ==="
bash scripts/install_launchagents.sh

echo ""
echo "Done. Watch logs:"
echo "  tail -f data/logs/brancher.log"
