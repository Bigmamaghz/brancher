from __future__ import annotations

from dataclasses import dataclass

from src.poll import PollResult, Signal
from src.schedule import today_et_str

URGENCY_RANK = {"scheduled": 0, "soon": 1, "in_play": 2}
HIT_JUMP = 0.05  # 5 percentage points


@dataclass(frozen=True)
class NewsItem:
    bot_id: str
    bot_name: str
    kind: str  # NEW | URGENCY | HIT_UP | ELIGIBLE | OFFLINE | ONLINE
    signal: Signal | None
    detail: str


def _snapshot(s: Signal) -> dict:
    return {
        "hit": s.hit,
        "urgency": s.urgency,
        "eligible": s.eligible,
        "enter_on": s.enter_on,
        "ticker": s.ticker,
        "event_type": s.event_type,
    }


def detect_news(
    results: list[PollResult],
    prior: dict[str, dict[str, dict]],
    prior_online: dict[str, bool],
    min_hit: float,
) -> tuple[list[NewsItem], dict[str, dict[str, dict]], dict[str, bool]]:
    """Compare poll to prior snapshot; return news + updated snapshot.

    Offline/online status is tracked but NOT emitted as news items — use
    ``cli doctor`` / daily fail digest for that. News items are only
    real signal changes (NEW, URGENCY, HIT_UP, ELIGIBLE, ENTER_TODAY).
    """
    news: list[NewsItem] = []
    today = today_et_str()
    new_snap: dict[str, dict[str, dict]] = {}
    new_online: dict[str, bool] = {}

    for r in results:
        new_online[r.bot_id] = r.error is None

        if r.error:
            # Keep prior snapshot if any; do not spam OFFLINE texts
            if r.bot_id in prior:
                new_snap[r.bot_id] = prior[r.bot_id]
            continue

        prev_bot = prior.get(r.bot_id, {})
        bot_snap: dict[str, dict] = {}

        for sig in r.signals:
            bot_snap[sig.id] = _snapshot(sig)

        # First poll for this bot: seed snapshot silently
        if not prev_bot:
            new_snap[r.bot_id] = bot_snap
            continue

        for sig in r.signals:
            old = prev_bot.get(sig.id)

            if old is None:
                if sig.eligible and sig.hit >= min_hit:
                    news.append(
                        NewsItem(
                            bot_id=r.bot_id,
                            bot_name=r.bot_name or r.bot_id,
                            kind="NEW",
                            signal=sig,
                            detail=f"new signal {sig.ticker}",
                        )
                    )
                continue

            if not old.get("eligible") and sig.eligible and sig.hit >= min_hit:
                news.append(
                    NewsItem(
                        bot_id=r.bot_id,
                        bot_name=r.bot_name or r.bot_id,
                        kind="ELIGIBLE",
                        signal=sig,
                        detail=f"now eligible {sig.ticker}",
                    )
                )

            old_rank = URGENCY_RANK.get(old.get("urgency", "scheduled"), 0)
            new_rank = URGENCY_RANK.get(sig.urgency, 0)
            if new_rank > old_rank and sig.eligible:
                news.append(
                    NewsItem(
                        bot_id=r.bot_id,
                        bot_name=r.bot_name or r.bot_id,
                        kind="URGENCY",
                        signal=sig,
                        detail=f"{sig.ticker} {old.get('urgency')} → {sig.urgency}",
                    )
                )

            if sig.enter_on == today and old.get("enter_on") != today and sig.eligible:
                news.append(
                    NewsItem(
                        bot_id=r.bot_id,
                        bot_name=r.bot_name or r.bot_id,
                        kind="ENTER_TODAY",
                        signal=sig,
                        detail=f"{sig.ticker} enter today",
                    )
                )

            old_hit = float(old.get("hit", 0))
            if sig.hit - old_hit >= HIT_JUMP and sig.eligible:
                news.append(
                    NewsItem(
                        bot_id=r.bot_id,
                        bot_name=r.bot_name or r.bot_id,
                        kind="HIT_UP",
                        signal=sig,
                        detail=f"{sig.ticker} {int(old_hit * 100)}% → {int(sig.hit * 100)}%",
                    )
                )

        new_snap[r.bot_id] = bot_snap

    return news, new_snap, new_online


def summarize_health(results: list[PollResult]) -> list[dict]:
    """Return a simple OK/FAIL table for each Author."""
    rows = []
    for r in results:
        rows.append(
            {
                "bot_id": r.bot_id,
                "bot_name": r.bot_name or r.bot_id,
                "ok": r.error is None,
                "signals": len(r.signals) if not r.error else 0,
                "error": r.error or "",
            }
        )
    return rows
