from src.news import detect_news
from src.poll import PollResult, Signal


def _sig(sig_id: str, ticker: str, hit: float = 0.88, urgency: str = "scheduled") -> Signal:
    return Signal(
        id=sig_id,
        ticker=ticker,
        side="UP",
        hit=hit,
        n=10,
        event_type="test",
        enter_on="2026-12-01",
        close_on="2026-12-02",
        eligible=True,
        urgency=urgency,
        sent_at="2026-08-24T12:00:00Z",
    )


def test_bootstrap_no_new_spam():
    results = [
        PollResult(
            bot_id="bot-a",
            bot_name="Bot A",
            updated_at="",
            signals=[_sig("1", "CME"), _sig("2", "JPM")],
        )
    ]
    news, snap, online = detect_news(results, {}, {}, min_hit=0.75)
    assert news == []
    assert len(snap["bot-a"]) == 2


def test_detects_new_signal_after_bootstrap():
    prior = {"bot-a": {"1": {"hit": 0.88, "urgency": "scheduled", "eligible": True, "enter_on": "2026-12-01", "ticker": "CME", "event_type": "test"}}}
    results = [
        PollResult(
            bot_id="bot-a",
            bot_name="Bot A",
            updated_at="",
            signals=[_sig("1", "CME"), _sig("2", "JPM")],
        )
    ]
    news, _, _ = detect_news(results, prior, {"bot-a": True}, min_hit=0.75)
    kinds = [n.kind for n in news]
    assert "NEW" in kinds
    assert any(n.signal and n.signal.ticker == "JPM" for n in news)
