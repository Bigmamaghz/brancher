from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

ADVANCE_NOTICE_DAYS = [7, 5, 3, 1]  # 1 = 24h


def today_et() -> date:
    return datetime.now(ET).date()


def today_et_str() -> str:
    return today_et().isoformat()


def parse_date(s: str) -> date:
    return date.fromisoformat(s)


def days_until(target: date, from_date: date | None = None) -> int:
    ref = from_date or today_et()
    return (target - ref).days


def advance_notice_label(days: int) -> str:
    if days == 1:
        return "24h"
    return f"{days}d"


def notices_due(enter_on: str, from_date: date | None = None) -> list[str]:
    """Return notice labels due today for a given enter_on date."""
    target = parse_date(enter_on)
    remaining = days_until(target, from_date)
    due = []
    for d in ADVANCE_NOTICE_DAYS:
        if remaining == d:
            due.append(advance_notice_label(d))
    return due


def is_enter_today(enter_on: str, from_date: date | None = None) -> bool:
    ref = from_date or today_et()
    return parse_date(enter_on) == ref


def is_sell_today(close_on: str, from_date: date | None = None) -> bool:
    ref = from_date or today_et()
    return parse_date(close_on) <= ref


def is_eod_time(hour: int = 16, minute: int = 0) -> bool:
    """True if current ET time is at or past market close."""
    now = datetime.now(ET)
    close = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now >= close
