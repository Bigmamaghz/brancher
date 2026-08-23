from __future__ import annotations

from dataclasses import dataclass

from src.poll import PollResult, Signal


@dataclass
class MergedSignal:
    signal: Signal
    bot_id: str
    bot_name: str


@dataclass
class SkippedSignal:
    signal: Signal
    bot_id: str
    bot_name: str
    reason: str
    winner_bot: str
    winner_hit: float


def merge_signals(results: list[PollResult]) -> tuple[list[MergedSignal], list[SkippedSignal]]:
    """Dedupe by ticker, keep highest hit. Losers become SKIP."""
    candidates: list[MergedSignal] = []

    for result in results:
        if result.error:
            continue
        for sig in result.signals:
            candidates.append(
                MergedSignal(signal=sig, bot_id=result.bot_id, bot_name=result.bot_name)
            )

    by_ticker: dict[str, list[MergedSignal]] = {}
    for c in candidates:
        by_ticker.setdefault(c.signal.ticker, []).append(c)

    winners: list[MergedSignal] = []
    losers: list[SkippedSignal] = []

    for ticker, group in by_ticker.items():
        group.sort(
            key=lambda m: (
                -m.signal.hit,
                m.signal.enter_on,
                m.bot_id,
            )
        )
        winner = group[0]
        winners.append(winner)

        for loser in group[1:]:
            losers.append(
                SkippedSignal(
                    signal=loser.signal,
                    bot_id=loser.bot_id,
                    bot_name=loser.bot_name,
                    reason=f"beaten by {winner.bot_name} on {ticker}",
                    winner_bot=winner.bot_name,
                    winner_hit=winner.signal.hit,
                )
            )

    return winners, losers
