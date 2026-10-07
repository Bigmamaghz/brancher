"""Decision logs. Nothing here places or cancels orders.

Sell rows (log_decision) append to <repo>/logs/decisions.csv:
  time, ticker, book_qty, live_available, market_open, decision, reason
decision is one of: sell | skip-guard | skip-closed | none.

Entry rows (log_entry_decision) append to a caller-supplied path, or
logs/entry_decisions.csv when no path is given. They stay off the sell file
so the two schemas are not mixed.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from src.config import REPO_ROOT

LOG_DIR = REPO_ROOT / "logs"
LOG_PATH = LOG_DIR / "decisions.csv"
ENTRY_LOG_PATH = LOG_DIR / "entry_decisions.csv"

_ENTRY_HEADER = (
    "time",
    "ticker",
    "pattern",
    "inventory_status",
    "joe_ok",
    "joe_verdict",
    "joe_source_count",
    "latency_s",
    "action",
    "reason",
)


def sell_decision(book_qty: int, avail: int | None, market_open: bool | None):
    """Pure decision function. Returns (decision, reason).

    Fail closed: unknown availability or unknown/closed market never sells.
    """
    if avail is None or avail <= 0:
        return "skip-guard", f"live available={avail} (<=0 or unknown)"
    if market_open is not True:
        return "skip-closed", f"market open={market_open} (closed or unknown)"
    return "sell", f"min(book {book_qty}, available {avail})"


def log_decision(ticker: str, book_qty: int, avail: int | None,
                 market_open: bool | None, decision: str, reason: str) -> None:
    """Append one CSV line. Never raises — logging must not break a cycle."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        line = ",".join([
            datetime.now(timezone.utc).isoformat(),
            str(ticker),
            str(book_qty),
            str(avail),
            str(market_open),
            str(decision),
            str(reason).replace(",", ";"),
        ])
        with LOG_PATH.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_entry_decision(
    ticker: str,
    pattern: str,
    inventory_status: str,
    joe_ok: str,
    joe_verdict: str,
    joe_source_count: str,
    latency_s: str,
    action: str,
    reason: str,
    path: str | Path | None = None,
) -> None:
    """Append one entry-gate row. Never raises — logging must not break a cycle."""
    try:
        dest = Path(path) if path else ENTRY_LOG_PATH
        dest.parent.mkdir(parents=True, exist_ok=True)
        new_file = not dest.exists()
        with dest.open("a", newline="") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow(_ENTRY_HEADER)
            writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                ticker,
                pattern,
                inventory_status,
                joe_ok,
                joe_verdict,
                joe_source_count,
                latency_s,
                action,
                str(reason).replace("\n", " "),
            ])
    except Exception:
        pass
