"""Fix-2 rule: only ONE position opens per ticker per day.

Two fleet patterns (fear_rs1m_strong + reclaim_sma20_fear, and sometimes
fear_voldry60) can fire on the same ticker and same closed bar. Merge dedups by
ticker within one poll batch, but two same-ticker signals must never both open.

This test drives Executor._process_enters with two MergedSignals for the SAME
ticker and asserts exactly one position opens.

Standalone:
  PYTHONPATH=. /Users/mybot/joe/trainvenv/bin/python tests/test_one_per_ticker.py
"""
from __future__ import annotations

from unittest.mock import patch

import src.executor as ex
from src.book import Book
from src.config import Settings
from src.merge import MergedSignal
from src.poll import Signal

TODAY = "2026-09-29"


def _settings() -> Settings:
    return Settings(
        alpaca_api_key="", alpaca_secret_key="", alpaca_base_url=None,
        telegram_bot_token="", telegram_chat_id="",
        paper_qty=1, paper_qty_max_mult=4,
        max_open_positions=10, max_opens_per_day=5, min_hit=0.75, min_n=30,
        telegram_news_only=True, telegram_eod=False, telegram_trades_only=False,
        joe_veto=False,
    )


def _sig(pattern: str, ticker: str = "ZZZ") -> Signal:
    return Signal(
        id=f"fleet-wave1-{pattern}-{ticker}-2026-09-28", ticker=ticker, side="UP",
        hit=0.557, n=115, event_type=pattern, enter_on=TODAY, close_on="2026-10-03",
        eligible=True, urgency="normal", sent_at="2026-09-29T00:00:00+00:00",
        net_r_full=0.108, net_r_recent=0.269, pattern_id=pattern,
    )


def _merged(sig: Signal) -> MergedSignal:
    return MergedSignal(signal=sig, bot_id="fleet-wave1", bot_name="Fleet · Wave1 Confirmed")


class _Telegram:
    def send(self, text, dry_run=False):
        return True


def test_two_same_ticker_signals_open_one_position():
    s = _settings()
    e = ex.Executor(s, dry_run=True)
    e._client = None
    e.telegram = _Telegram()
    book = Book()

    winners = [_merged(_sig("fear_rs1m_strong")), _merged(_sig("reclaim_sma20_fear"))]
    # Both pass gate (n=115, net R both positive); one-per-ticker must block the 2nd.
    with patch.object(ex, "submit_buy", return_value="oid"):
        e._process_enters(winners, book, TODAY)

    assert len(book.positions) == 1, f"expected 1 position, got {len(book.positions)}"
    assert book.positions[0].ticker == "ZZZ"


def test_three_same_ticker_signals_open_one_position():
    s = _settings()
    e = ex.Executor(s, dry_run=True)
    e._client = None
    e.telegram = _Telegram()
    book = Book()

    winners = [
        _merged(_sig("fear_rs1m_strong")),
        _merged(_sig("reclaim_sma20_fear")),
        _merged(_sig("fear_voldry60")),
    ]
    with patch.object(ex, "submit_buy", return_value="oid"):
        e._process_enters(winners, book, TODAY)

    assert len(book.positions) == 1, f"expected 1 position, got {len(book.positions)}"


def test_different_tickers_open_two_positions():
    s = _settings()
    e = ex.Executor(s, dry_run=True)
    e._client = None
    e.telegram = _Telegram()
    book = Book()

    winners = [_merged(_sig("fear_rs1m_strong", "ZZZ")),
               _merged(_sig("fear_rs1m_strong", "YYY"))]
    with patch.object(ex, "submit_buy", return_value="oid"):
        e._process_enters(winners, book, TODAY)

    assert len(book.positions) == 2, f"expected 2 positions, got {len(book.positions)}"


def _run_standalone():
    import traceback
    tests = [("test_two_same_ticker_signals_open_one_position", test_two_same_ticker_signals_open_one_position),
             ("test_three_same_ticker_signals_open_one_position", test_three_same_ticker_signals_open_one_position),
             ("test_different_tickers_open_two_positions", test_different_tickers_open_two_positions)]
    results = []
    for name, fn in tests:
        try:
            fn(); results.append((name, "PASS", ""))
        except AssertionError as e:
            results.append((name, "FAIL", str(e)))
        except Exception as e:
            results.append((name, "ERROR", f"{type(e).__name__}: {e}\n" + traceback.format_exc()))
    print("=" * 60)
    print("one-position-per-ticker-per-day — no orders")
    print("=" * 60)
    for name, status, detail in results:
        print(f"[{status}] {name}")
        if detail:
            for line in detail.rstrip().splitlines():
                print(f"        {line}")
    npass = sum(1 for _, s, _ in results if s == "PASS")
    print("-" * 60)
    print(f"{npass}/{len(results)} passed")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
