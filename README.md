# Brancher

Universal portfolio controller for all trading bots. One repo, one Alpaca paper account, one Telegram chat.

**Authors** (many) publish signals via `GET /signals`. **Brancher** (one) polls them, merges, sizes, trades on Alpaca paper, and texts you — always with `SOURCE: {bot name}`.

```
AUTHOR BOTS (many)  →  BRANCHER (one)  →  Telegram + Alpaca paper
research + /signals      merge, risk, execute     one pocket
```

## Hard rules

- Paper Alpaca only — live broker URLs are refused at startup
- Research only — every Telegram message ends with the disclaimer
- Authors never hold Alpaca or portfolio Telegram keys
- Only Brancher sends ENTER / SELL / EOD / SKIP
- Author servers bind `127.0.0.1` only

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env          # fill Alpaca paper + Telegram credentials
cp config/bots.yaml.example config/bots.yaml   # if missing

python -m src.cli keys --bot financials-xlf     # generate Author API key
python -m src.cli ping                          # verify Telegram
python -m src.cli run --dry-run                 # smoke test (no orders)
python -m src.cli run                           # full 15-min loop
```

## CLI

| Command | Description |
|---------|-------------|
| `python -m src.cli run` | Full portfolio loop (every 15 min) |
| `python -m src.cli run --dry-run` | Loop without Alpaca orders |
| `python -m src.cli poll` | Poll all bots once |
| `python -m src.cli status` | Show book + open positions |
| `python -m src.cli ping` | Send test Telegram message |
| `python -m src.cli keys --bot <id>` | Generate Author API key |

## Configuration

- **`.env`** — Alpaca paper keys, Telegram, sizing/risk tunables, per-bot API keys
- **`config/bots.yaml`** — register Authors (id, name, base_url, api_key_env)

Add new bots by editing `bots.yaml` and adding the matching `*_API_KEY` to `.env`. See [INTEGRATION.md](INTEGRATION.md).

## Mac Mini (LaunchAgent)

**One command — paste in Terminal on your Mac Mini:**

```bash
curl -fsSL https://raw.githubusercontent.com/Bigmamaghz/brancher/main/scripts/setup-mac-mini.sh | bash
```

Or if you already have the repo:

```bash
cd ~/brancher   # or: cd "/Users/josephghouzi/resulter trader"
bash scripts/setup-mac-mini.sh
```

Double-click **`Start Brancher.command`** in Finder (same thing).

Runs `python -m src.cli run` at login, keeps alive, logs to `data/logs/brancher.log`.

```bash
bash scripts/install_launchagents.sh   # LaunchAgent only (if already set up)
```

## Data

| Path | Purpose |
|------|---------|
| `data/portfolio/book.json` | Open positions, daily opens, notice state |
| `data/portfolio/bots/<id>/last_seen.json` | Last poll result per bot |
| `data/portfolio/bots/<id>/signals.jsonl` | Signal history per bot |

## Tests

```bash
pytest -q
```

## Adding an Author

See [INTEGRATION.md](INTEGRATION.md) for the full Author contract.
