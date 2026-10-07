"""Per-cycle sell decision log (one line per ticker per cycle).

Untracked by git: writes to <repo>/logs/decisions.csv. Fields:
  time, ticker, book_qty, live_available, market_open, decision, reason

decision is one of: sell | skip-guard | skip-closed | none.
Nothing here places or cancels orders — it only records the decision.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.config import REPO_ROOT

LOG_DIR = REPO_ROOT / "logs"
LOG_PATH = LOG_DIR / "decisions.csv"


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
