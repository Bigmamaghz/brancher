# Author Integration Guide

Any trading repo can become a Brancher **Author** by implementing a read-only signals endpoint. Brancher handles all execution, sizing, risk, and Telegram notifications.

## Architecture

```
Your Author repo                    Brancher (this repo)
─────────────────                   ────────────────────
Research + signals only             Polls /signals every 15 min
GET /signals (Bearer auth)    →     Merges, sizes, trades, texts
127.0.0.1 only                      Alpaca paper + Telegram keys
BRANCHER_API_KEY in .env            All secrets live here only
```

## Step 1 — Implement `GET /signals`

Bind your HTTP server to **`127.0.0.1` only** (never `0.0.0.0`).

Protect the endpoint with Bearer token auth:

```
Authorization: Bearer <BRANCHER_API_KEY>
```

Return JSON matching this contract:

```json
{
  "bot_id": "financials-xlf",
  "bot_name": "Financials · XLF",
  "updated_at": "2026-08-23T12:00:00Z",
  "signals": [
    {
      "id": "stable-id-per-signal",
      "ticker": "CME",
      "side": "UP",
      "hit": 0.88,
      "n": 17,
      "event_type": "material_agreement",
      "enter_on": "2026-11-16",
      "close_on": "2026-11-17",
      "pattern_id": "optional",
      "sec_url": "optional",
      "eligible": true,
      "urgency": "scheduled",
      "sent_at": "2026-08-23T10:00:00Z"
    }
  ]
}
```

### Field reference

| Field | Required | Description |
|-------|----------|-------------|
| `id` | yes | Stable unique ID (used for dedup and notice tracking) |
| `ticker` | yes | Stock symbol |
| `side` | yes | `"UP"` (long only for now) |
| `hit` | yes | Confidence 0.0–1.0 (e.g. 0.88 = 88%) |
| `n` | yes | Sample size |
| `event_type` | yes | Human-readable event label |
| `enter_on` | yes | Entry date `YYYY-MM-DD` |
| `close_on` | yes | Exit date `YYYY-MM-DD` |
| `eligible` | yes | `false` to exclude from trading |
| `urgency` | yes | `scheduled` \| `soon` \| `in_play` |
| `pattern_id` | no | Optional pattern reference |
| `sec_url` | no | Optional SEC filing URL |
| `sent_at` | yes | ISO8601 timestamp |

### Urgency

| Value | Meaning |
|-------|---------|
| `scheduled` | Future entry; Brancher sends advance notices (7d, 5d, 3d, 24h) |
| `soon` | Entry imminent; Brancher may ENTER |
| `in_play` | Active now; Brancher may ENTER |

Brancher enters when `urgency` is `in_play` or `soon`, or when `enter_on` is today (ET).

## Step 2 — API key

Generate a key from Brancher:

```bash
python -m src.cli keys --bot your-bot-id
```

Output example:

```
FINANCIALS_XLF_API_KEY=financials_xlf_a1b2c3d4...
```

- **Brancher `.env`**: paste as `FINANCIALS_XLF_API_KEY=...` (env var name from `api_key_env` in bots.yaml)
- **Author `.env`**: paste as `BRANCHER_API_KEY=...` (same value, different var name)

Key format: `{bot_id_with_underscores}_{64-char-hex}` — hyphens in bot ID become underscores.

## Step 3 — Register in Brancher

Edit `config/bots.yaml`:

```yaml
bots:
  - id: your-bot-id
    name: "Your Bot · Display Name"
    base_url: "http://127.0.0.1:8780"
    signals_path: "/signals"
    api_key_env: "YOUR_BOT_API_KEY"
    enabled: true
```

Add matching key to Brancher `.env`:

```
YOUR_BOT_API_KEY=your_bot_id_<hex>
```

Restart Brancher (or wait for next poll cycle). No code changes needed in Brancher.

## Author hard rules

1. **Never** hold Alpaca keys or portfolio Telegram credentials
2. **Never** print `BUY` or send trade Telegram messages — Brancher does that
3. **Never** bind to `0.0.0.0` — `127.0.0.1` only
4. **Research only** — your repo produces signals; Brancher executes

## What Brancher does with your signals

1. Polls `GET /signals` every 15 minutes
2. Logs to `data/portfolio/bots/<your-id>/`
3. Merges across all bots — same ticker, highest `hit` wins; losers get SKIP
4. Sends advance notices before `enter_on`
5. ENTER on `in_play` / `soon` / today (capped by daily and position limits)
6. SELL when `close_on <= today`
7. Every Telegram message includes `SOURCE: Your Bot · Display Name`

## Minimal Flask example (Author side)

```python
import os
from flask import Flask, jsonify, request
from secrets import compare_digest

app = Flask(__name__)
API_KEY = os.environ["BRANCHER_API_KEY"]

@app.route("/signals")
def signals():
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if not compare_digest(token, API_KEY):
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({
        "bot_id": "your-bot-id",
        "bot_name": "Your Bot",
        "updated_at": "2026-08-23T12:00:00Z",
        "signals": [],
    })

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8780)
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| 401 from Author | Check `BRANCHER_API_KEY` matches Brancher's `*_API_KEY` |
| Bot not polled | `enabled: true` in bots.yaml; check Brancher logs |
| Signal skipped | Another bot won same ticker with higher `hit`; or below `MIN_HIT` |
| No ENTER | Check `urgency`, `enter_on`, `eligible`, position/day caps |
