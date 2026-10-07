"""levels — deterministic playbook stop/target, computed ONCE at entry (fix 3).

Fix 3: a position's stop and target are frozen at entry from the ATR at that
moment and stored on the position record. Nothing recalculates them later.

Formulas are Joseph's playbook (identical to joe_levels.levels):
  BUY  : sl = entry - max(atr, entry*0.005);  tp = entry + rr*(entry - sl)
  SHORT: sl = entry + max(atr, entry*0.005);  tp = entry - rr*(sl - entry)

Data access is READ-ONLY daily bars from data.alpaca.markets (IEX feed). This
module never touches /v2/orders or any trading endpoint.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import REPO_ROOT

RR_DEFAULT = 2.0
BARS_DIR = REPO_ROOT / "data" / "levels_bars"
DATA_BASE = "https://data.alpaca.markets/v2/stocks/bars"


def playbook_levels(side: str, entry: float, atr: float, rr: float = RR_DEFAULT) -> tuple[float, float]:
    """Return (stop, target) rounded to cents. Same math as joe_levels.levels."""
    s = side.strip().upper()
    floor = entry * 0.005
    risk = max(atr, floor)
    if s in ("BUY", "LONG", "UP"):
        stop = entry - risk
        target = entry + rr * (entry - stop)
    elif s in ("SHORT", "SELL", "DOWN"):
        stop = entry + risk
        target = entry - rr * (stop - entry)
    else:
        raise ValueError(f"unknown side: {side!r}")
    return round(stop, 2), round(target, 2)


def atr14(bars: list[dict], i: int | None = None, n: int = 14) -> float | None:
    """Average true range over the last n bars ending at index i (default last)."""
    if not bars:
        return None
    if i is None:
        i = len(bars) - 1
    if i < n:
        return None
    trs = []
    for k in range(i - n + 1, i + 1):
        h, l = bars[k]["h"], bars[k]["l"]
        pc = bars[k - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / n


def _headers(settings) -> dict:
    return {
        "APCA-API-KEY-ID": settings.alpaca_api_key,
        "APCA-API-SECRET-KEY": settings.alpaca_secret_key,
    }


def fetch_daily_bars(ticker: str, settings, cache: bool = True) -> list[dict]:
    """Fetch ~1y of daily bars (IEX, split-adjusted) for ticker. Read-only data API."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)
    h = _headers(settings)
    rows: list[dict] = []
    tok = None
    while True:
        url = (
            f"{DATA_BASE}?symbols={urllib.parse.quote(ticker)}&timeframe=1Day"
            f"&start={start.strftime('%Y-%m-%dT00:00:00Z')}&end={end.strftime('%Y-%m-%dT23:59:00Z')}"
            "&limit=1000&feed=iex&adjustment=split"
        )
        if tok:
            url += f"&page_token={urllib.parse.quote(tok)}"
        req = urllib.request.Request(url, headers=h)
        data = json.load(urllib.request.urlopen(req, timeout=45))
        page = data.get("bars", {})
        page = page.get(ticker, []) if isinstance(page, dict) else page
        rows.extend(page)
        tok = data.get("next_page_token")
        if not tok:
            break
    rows.sort(key=lambda b: b["t"])  # oldest -> newest
    if cache and rows:
        BARS_DIR.mkdir(parents=True, exist_ok=True)
        (BARS_DIR / f"{ticker}.json").write_text(json.dumps(rows))
    return rows


def bars_for(ticker: str, settings) -> list[dict]:
    """Cached bars if present, else fetch (read-only)."""
    f = BARS_DIR / f"{ticker}.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    return fetch_daily_bars(ticker, settings)


def atr_at_entry(ticker: str, settings) -> float | None:
    """ATR14 from the latest closed daily bar. Read-only."""
    try:
        return atr14(bars_for(ticker, settings))
    except Exception:
        return None


def freeze_levels(side: str, entry: float, atr: float | None) -> tuple[float | None, float | None]:
    """Compute the frozen (stop, target). ATR floor = 0.5% of entry if ATR missing."""
    if entry is None or entry <= 0:
        return None, None
    a = atr if (atr and atr > 0) else entry * 0.005
    return playbook_levels(side, entry, a)
