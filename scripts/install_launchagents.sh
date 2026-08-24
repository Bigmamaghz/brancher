#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_NAME="com.bigmamaghz.brancher.plist"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME"
VENV_PYTHON="$REPO_ROOT/.venv/bin/python3"

if [[ ! -x "$VENV_PYTHON" ]]; then
  VENV_PYTHON="$REPO_ROOT/.venv/bin/python"
fi
if [[ ! -x "$VENV_PYTHON" ]]; then
  VENV_PYTHON="$(command -v python3)"
fi

mkdir -p "$REPO_ROOT/data/logs"
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.bigmamaghz.brancher</string>
  <key>ProgramArguments</key>
  <array>
    <string>${VENV_PYTHON}</string>
    <string>-m</string>
    <string>src.cli</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${REPO_ROOT}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${REPO_ROOT}/data/logs/brancher.log</string>
  <key>StandardErrorPath</key>
  <string>${REPO_ROOT}/data/logs/brancher.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
  </dict>
</dict>
</plist>
EOF

launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

echo "Installed LaunchAgent: $PLIST_PATH"
echo "Logs: $REPO_ROOT/data/logs/brancher.log"
echo ""
echo "Commands:"
echo "  launchctl start com.bigmamaghz.brancher"
echo "  launchctl stop com.bigmamaghz.brancher"
echo "  tail -f $REPO_ROOT/data/logs/brancher.log"
