#!/usr/bin/env bash
# Start Brancher portfolio loop on this machine.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -f .env ]]; then
  echo "Missing .env — copy from .env.example and fill in Alpaca + Telegram."
  exit 1
fi

if [[ ! -f config/bots.yaml ]]; then
  cp config/bots.yaml.example config/bots.yaml
  echo "Created config/bots.yaml from example."
fi

if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PYTHON="$REPO_ROOT/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

DRY_RUN=""
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN="--dry-run"
  shift
fi

echo "Brancher starting (python=$PYTHON, dry_run=${DRY_RUN:-no})"
exec "$PYTHON" -m src.cli run $DRY_RUN "$@"
