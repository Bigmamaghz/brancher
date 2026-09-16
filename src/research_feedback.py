"""Read-only Pattern Fleet research feedback for Brancher reporting.

Feedback is deliberately not consulted by signal selection or order execution.
Malformed and stale records are ignored so an incomplete research export cannot
change portfolio behavior.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import DATA_DIR

logger = logging.getLogger(__name__)
FEEDBACK_DIR = DATA_DIR / "research_feedback"
FEEDBACK_PATH = FEEDBACK_DIR / "feedback.jsonl"
STALE_AFTER_DAYS = 30
STATUSES = {"active", "decaying", "dead"}


@dataclass(frozen=True)
class ResearchFeedback:
    ticker: str
    pattern: str
    status: str
    evidence_refs: list[str]
    recent_results: Any
    oos_results: Any
    regime_results: Any
    managed_results: Any
    last_seen: str
    lesson: str

    @property
    def key(self) -> str:
        return f"{self.ticker}:{self.pattern}"


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _record(raw: Any, now: datetime) -> ResearchFeedback | None:
    if not isinstance(raw, dict):
        return None
    ticker = raw.get("ticker")
    pattern = raw.get("pattern")
    status = raw.get("status")
    last_seen = raw.get("last_seen")
    if (
        not isinstance(ticker, str) or not ticker.strip()
        or not isinstance(pattern, str) or not pattern.strip()
        or status not in STATUSES
    ):
        return None
    seen = _parse_timestamp(last_seen)
    if seen is None or seen > now or (now - seen).days > STALE_AFTER_DAYS:
        return None
    refs = raw.get("evidence_refs", [])
    if not isinstance(refs, list) or not all(isinstance(ref, str) and ref for ref in refs):
        return None
    lesson = raw.get("lesson", "")
    if not isinstance(lesson, str):
        return None
    return ResearchFeedback(
        ticker=ticker.strip().upper(), pattern=pattern.strip(), status=status,
        evidence_refs=refs, recent_results=raw.get("recent_results"),
        oos_results=raw.get("oos_results"), regime_results=raw.get("regime_results"),
        managed_results=raw.get("managed_results"), last_seen=last_seen,
        lesson=lesson,
    )


def load_feedback(path: Path | None = None, *, now: datetime | None = None) -> list[ResearchFeedback]:
    """Load the newest valid record for each ticker+pattern; never raise on bad input."""
    source = path or FEEDBACK_PATH
    if not source.exists():
        return []
    current = now or datetime.now(timezone.utc)
    latest: dict[str, ResearchFeedback] = {}
    try:
        with source.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    parsed = _record(json.loads(line), current)
                except (json.JSONDecodeError, TypeError, ValueError):
                    parsed = None
                if parsed is None:
                    logger.warning("Ignoring malformed or stale research feedback at %s:%d", source, line_no)
                    continue
                previous = latest.get(parsed.key)
                if previous is None or _parse_timestamp(parsed.last_seen) > _parse_timestamp(previous.last_seen):
                    latest[parsed.key] = parsed
    except (OSError, UnicodeError) as exc:
        logger.warning("Ignoring unreadable research feedback %s: %s", source, exc)
    return sorted(latest.values(), key=lambda item: (item.ticker, item.pattern))


def feedback_report(path: Path | None = None) -> dict[str, Any]:
    records = load_feedback(path)
    by_status = {status: sum(record.status == status for record in records) for status in sorted(STATUSES)}
    return {
        "records": [record.__dict__ for record in records],
        "count": len(records),
        "by_status": by_status,
        "source": str(path or FEEDBACK_PATH),
        "stale_after_days": STALE_AFTER_DAYS,
    }
