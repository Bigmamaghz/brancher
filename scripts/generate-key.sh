#!/usr/bin/env bash
# Generate an Author API key for Brancher.
# Usage: ./scripts/generate-key.sh <bot-id>
# Example: ./scripts/generate-key.sh financials-jpm

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <bot-id>"
  echo "Example: $0 financials-jpm"
  exit 1
fi

BOT_ID="$1"

if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PYTHON="$REPO_ROOT/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

echo ""
echo "=== Brancher API key for: $BOT_ID ==="
echo ""
"$PYTHON" -m src.cli keys --bot "$BOT_ID"
echo ""
echo "Next steps:"
echo "  1. Add the Brancher line to your .env (if new bot)"
echo "  2. Register bot in config/bots.yaml (if new bot)"
echo "  3. Send ONBOARD_MESSAGE below to the Author developer"
echo "  4. See scripts/ONBOARD_MESSAGE.txt for copy-paste text"
echo ""
