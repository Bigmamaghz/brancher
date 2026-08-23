from src.merge import merge_signals
from src.poll import PollResult, Signal


def _signal(ticker: str, hit: float, bot_suffix: str = "a") -> Signal:
    return Signal(
        id=f"sig-{ticker}-{bot_suffix}",
        ticker=ticker,
        side="UP",
        hit=hit,
        n=10,
        event_type="test_event",
        enter_on="2026-11-16",
        close_on="2026-11-17",
        eligible=True,
        urgency="scheduled",
        sent_at="2026-08-23T10:00:00Z",
    )


def test_merge_dedupe_keeps_highest_hit():
    results = [
        PollResult(
            bot_id="bot-a",
            bot_name="Bot A",
            updated_at="2026-08-23T12:00:00Z",
            signals=[_signal("CME", 0.80, "a")],
        ),
        PollResult(
            bot_id="bot-b",
            bot_name="Bot B",
            updated_at="2026-08-23T12:00:00Z",
            signals=[_signal("CME", 0.92, "b")],
        ),
    ]
    winners, losers = merge_signals(results)

    assert len(winners) == 1
    assert winners[0].bot_name == "Bot B"
    assert winners[0].signal.hit == 0.92

    assert len(losers) == 1
    assert losers[0].bot_name == "Bot A"
    assert "beaten by Bot B" in losers[0].reason
    assert losers[0].winner_hit == 0.92


def test_merge_different_tickers_both_win():
    results = [
        PollResult(
            bot_id="bot-a",
            bot_name="Bot A",
            updated_at="",
            signals=[_signal("CME", 0.80)],
        ),
        PollResult(
            bot_id="bot-b",
            bot_name="Bot B",
            updated_at="",
            signals=[_signal("JPM", 0.90)],
        ),
    ]
    winners, losers = merge_signals(results)
    assert len(winners) == 2
    assert len(losers) == 0
