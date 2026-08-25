#!/usr/bin/env bash
# Find what's actually listening and which Authors Brancher can reach.
set -euo pipefail

cd "$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Ports 8780-8790 listening (this machine) ==="
if command -v lsof >/dev/null; then
  lsof -nP -iTCP:8780-8790 -sTCP:LISTEN 2>/dev/null || echo "(nothing listening)"
else
  echo "lsof not available"
fi

echo ""
echo "=== Probe each port (HTTP status) ==="
for p in 8780 8781 8782 8783 8784 8785; do
  code=$(curl -s -o /tmp/brancher_probe_$p.body -w "%{http_code}" --connect-timeout 2 "http://127.0.0.1:$p/signals" 2>/dev/null || echo "000")
  bytes=$(wc -c < /tmp/brancher_probe_$p.body 2>/dev/null | tr -d ' ' || echo 0)
  echo "  127.0.0.1:$p  HTTP $code  bytes=$bytes"
  if [[ "$code" == "401" || "$code" == "200" ]]; then
    head -c 120 /tmp/brancher_probe_$p.body 2>/dev/null; echo
  fi
done

echo ""
echo "=== Brancher config expects ==="
if [[ -f config/bots.yaml ]]; then
  python3 - <<'PY'
import yaml
from pathlib import Path
data = yaml.safe_load(Path("config/bots.yaml").read_text()) or {}
for b in data.get("bots", []):
    print(f"  {b.get('id')}: enabled={b.get('enabled')} url={b.get('base_url')}{b.get('signals_path','/signals')}")
PY
else
  echo "  config/bots.yaml missing — copy from example"
fi

echo ""
echo "=== Brancher doctor ==="
if [[ -x .venv/bin/python3 ]]; then
  .venv/bin/python3 -m src.cli doctor || true
else
  python3 -m src.cli doctor || true
fi

echo ""
echo "=== Diagnosis ==="
python3 - <<'PY'
import subprocess, re
from pathlib import Path

listening = set()
try:
    out = subprocess.check_output(["lsof", "-nP", "-iTCP:8780-8790", "-sTCP:LISTEN"], text=True, stderr=subprocess.DEVNULL)
    for m in re.finditer(r":(878\d)\b", out):
        listening.add(int(m.group(1)))
except Exception:
    pass

import urllib.request
reachable = {}
for p in range(8780, 8786):
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{p}/signals", method="GET")
        with urllib.request.urlopen(req, timeout=2) as r:
            reachable[p] = r.status
    except Exception as e:
        # 401 still means server is up
        err = str(e)
        if "401" in err:
            reachable[p] = 401
        else:
            reachable[p] = None

print(f"Listening ports: {sorted(listening) or 'NONE'}")
print(f"HTTP-up ports:   {[p for p,s in reachable.items() if s] or 'NONE'}")

expected = {8780: "financials-xlf", 8781: "candlestick-patterns", 8783: "technology-xlk"}
for port, bot in expected.items():
    if reachable.get(port):
        print(f"  FIX NOT NEEDED: {bot} responds on {port} (HTTP {reachable[port]})")
    elif port in listening:
        print(f"  PARTIAL: {bot} port {port} is listening but /signals did not respond cleanly")
    else:
        print(f"  ROOT CAUSE: {bot} — NOTHING on 127.0.0.1:{port} on THIS Mac Mini")
        print(f"             → Author is not running here (wrong machine, wrong port, or process dead)")
PY
