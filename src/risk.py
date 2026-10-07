from __future__ import annotations

from src.config import Settings
from src.merge import MergedSignal
from src.poll import Signal


def check_eligible(signal: Signal, settings: Settings) -> str | None:
    """Return skip reason if signal fails risk checks, else None.

    Gate = MIN_N floor + net avg R > 0 on BOTH full history and trailing 365d
    (replaces the old win%-vs-XLV MIN_HIT gate, per fix 2).
    """
    if not signal.eligible:
        return "not eligible"
    if signal.n < settings.min_n:
        return f"n {signal.n} below MIN_N {settings.min_n}"
    if signal.net_r_full is None or signal.net_r_recent is None:
        return "net-R unavailable"
    if signal.net_r_full <= 0 or signal.net_r_recent <= 0:
        return (f"net-R not positive both windows "
                f"(full {signal.net_r_full:+.3f}, recent {signal.net_r_recent:+.3f})")
    if signal.side != "UP":
        return f"unsupported side {signal.side}"
    return None


def can_open_position(
    open_count: int,
    opens_today: int,
    settings: Settings,
) -> str | None:
    if open_count >= settings.max_open_positions:
        return f"MAX_OPEN_POSITIONS ({settings.max_open_positions}) reached"
    if opens_today >= settings.max_opens_per_day:
        return f"MAX_OPENS_PER_DAY ({settings.max_opens_per_day}) reached"
    return None


def should_enter(merged: MergedSignal, today_str: str) -> bool:
    sig = merged.signal
    from src.schedule import parse_date
    # Stale-window guard: never enter a signal whose entry date already passed.
    # Overdue signals forced buy-at-open + immediate sell (slippage, no edge).
    if parse_date(sig.enter_on) < parse_date(today_str):
        return False
    if sig.urgency in ("in_play", "soon") and parse_date(sig.enter_on) <= parse_date(today_str):
        return True
    if sig.enter_on == today_str:
        return True
    return False
