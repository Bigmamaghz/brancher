#!/usr/bin/env bash
# Install Brancher deps on Mac Mini (works with old system pip/Python 3.9).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if [[ -x "$REPO_ROOT/.venv/bin/python3" ]]; then
    PYTHON="$REPO_ROOT/.venv/bin/python3"
  else
    PYTHON="$(command -v python3)"
  fi
fi

echo "Using Python: $PYTHON ($("$PYTHON" --version 2>&1))"

"$PYTHON" -m pip install --upgrade pip setuptools wheel
# Prefer editable install; fall back to plain deps (still runs via python -m src.cli)
if ! "$PYTHON" -m pip install -e ".[dev]"; then
  echo "Editable install failed — installing dependencies directly..."
  "$PYTHON" -m pip install "httpx>=0.27" "pyyaml>=6.0" "python-dotenv>=1.0" "alpaca-py>=0.33" "pytest>=8.0"
fi

echo "Install OK."
"$PYTHON" -c "import dotenv, httpx, yaml; print('imports ok')"
