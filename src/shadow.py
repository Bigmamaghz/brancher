"""shadow.py — pattern benching / shadow tracking.

Losing patterns are not deleted: once a pattern (event_type family) has enough
closed paper trades and a negative average realized P/L, it is BENCHED —
Brancher stops trading it but keeps logging its signals to the shadow file so
we can still watch how it would have done.

Signal id format: TICKER:event|flags...:enter_on[:tag] — the pattern family is
the first event token (before any '|' separator).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from src.config import BOOK_PATH, ensure_data_dirs

SHADOW_PATH = BOOK_PATH.parent / "shadow.jsonl"

# Bench a pattern after this many closed trades if avg realized P/L is negative.
BENCH_MIN_TRADES = 5
BENCH_MAX_AVG_PL = 0.0


def event_family(signal_id: str) -> str:
    """'CINF:insider|already_ran|xlf_down:...' -> 'insider'"""
    parts = (signal_id or "").split(":")
    if len(parts) < 2:
        return "unknown"
    return parts[1].split("|")[0] or "unknown"


def record_closed(book, ticker: str, bot_id: str, signal_id: str,
                  enter_on: str, close_on: str, entry_px: float | None,
                  exit_px: float | None, closed_at: str) -> dict:
    pl_pct = None
    if entry_px and exit_px and entry_px > 0:
        pl_pct = round((exit_px - entry_px) / entry_px * 100, 3)
    rec = {
        "ticker": ticker,
        "bot_id": bot_id,
        "signal_id": signal_id,
        "event_family": event_family(signal_id),
        "enter_on": enter_on,
        "close_on": close_on,
        "entry_px": entry_px,
        "exit_px": exit_px,
        "pl_pct": pl_pct,
        "closed_at": closed_at,
    }
    book.record_closed(rec)
    ensure_data_dirs()
    with open(SHADOW_PATH, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def pattern_stats(book) -> dict[str, dict]:
    """Per event-family realized stats across all bots."""
    out: dict[str, list[float]] = {}
    for rec in book.closed:
        fam = rec.get("event_family", "unknown")
        if rec.get("pl_pct") is not None:
            out.setdefault(fam, []).append(float(rec["pl_pct"]))
    return {
        fam: {
            "trades": len(pls),
            "avg_pl_pct": round(sum(pls) / len(pls), 3),
            "wins": sum(1 for p in pls if p > 0),
        }
        for fam, pls in out.items()
    }


def benched_patterns(book) -> set[str]:
    """Families with enough history and a negative average — still shadowed."""
    stats = pattern_stats(book)
    return {
        fam for fam, s in stats.items()
        if s["trades"] >= BENCH_MIN_TRADES and s["avg_pl_pct"] < BENCH_MAX_AVG_PL
    }


def bench_reason(book, signal_id: str) -> str | None:
    fam = event_family(signal_id)
    if fam in benched_patterns(book):
        stats = pattern_stats(book)[fam]
        return (
            f"pattern benched — shadow only ({fam}: {stats['trades']} trades, "
            f"avg {stats['avg_pl_pct']:+.2f}%, {stats['wins']} wins)"
        )
    return None