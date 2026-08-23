from __future__ import annotations

from src.config import Settings
from src.merge import MergedSignal
from src.poll import Signal


def check_eligible(signal: Signal, settings: Settings) -> str | None:
    """Return skip reason if signal fails risk checks, else None."""
    if not signal.eligible:
        return "not eligible"
    if signal.hit < settings.min_hit:
        return f"hit {signal.hit:.0%} below MIN_HIT {settings.min_hit:.0%}"
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
    if sig.urgency in ("in_play", "soon"):
        return True
    if sig.enter_on == today_str:
        return True
    return False
