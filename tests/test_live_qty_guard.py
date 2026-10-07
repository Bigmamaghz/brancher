"""Mocked-Alpaca tests for the live-quantity sell guard (Option D).

ZERO real orders: every Alpaca interaction is a mock. The guard is
src.alpaca.get_position_qty() wired in before every submit_sell
(executor._process_sells and exit_monitor.run_exit_monitor).

Assertions:
  (1) live short (qty_available < 0)  -> no order
  (2) live 0 (flat)                   -> no order
  (3) API error (get_position_qty None) -> no order (fail closed)
  (4) live less than book             -> sells min(book qty, available)
  (5) resting stop leaves qty_available 0 -> no order
  (6) CL/SCHW dry-run replay          -> zero orders

Runnable: pytest tests/test_live_qty_guard.py
"""
from __future__ import annotations

import json
from unittest.mock import patch

import src.executor as ex
import src.exit_monitor as em
from src.book import Book, Position
from src.config import Settings

TODAY = "2026-10-06"


# --------------------------------------------------------------------------- #
# Mock Alpaca surface
# --------------------------------------------------------------------------- #
class MockPos:
    """Mirrors the alpaca Position fields the guard/entry-price read."""

    def __init__(self, symbol: str, qty: int, qty_available: int | None = None):
        self.symbol = symbol
        self.qty = qty
        self.qty_available = qty if qty_available is None else qty_available
        self.avg_entry_price = 100.0


class MockOrder:
    def __init__(self, status, fill):
        self.status = status
        self.filled_avg_price = fill


class MockClient:
    """Positions from a list; optional hard error to exercise the None path."""

    def __init__(self, positions=None, error: bool = False):
        self._positions = positions or []
        self._error = error
        self.sells: list[tuple] = []

    def get_all_positions(self):
        if self._error:
            raise RuntimeError("alpaca api down")
        return list(self._positions)

    def get_orders(self, filter=None):
        return []

    def cancel_order(self, oid):
        pass

    def get_order_by_id(self, oid):
        return MockOrder("filled", 79.5)


class RecordingTelegram:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, text, dry_run=False):
        self.sent.append(text)
        return True


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
def _settings() -> Settings:
    return Settings(
        alpaca_api_key="", alpaca_secret_key="", alpaca_base_url=None,
        telegram_bot_token="", telegram_chat_id="",
        paper_qty=1, paper_qty_max_mult=1,
        max_open_positions=10, max_opens_per_day=5, min_hit=0.75, min_n=30,
        telegram_news_only=True, telegram_eod=False, telegram_trades_only=False,
        joe_veto=False,
    )


def _book(ticker: str = "CL", qty: int = 23) -> Book:
    b = Book()
    b.positions.append(Position(
        ticker=ticker, qty=qty, bot_id="candlestick-patterns",
        bot_name="Candlestick \u00b7 Pattern Finder",
        signal_id=f"candlestick-patterns-{ticker}-x-2026-09-28",
        enter_on="2026-09-28", close_on="2026-10-03",
        opened_at="2026-09-28T13:33:00+00:00"))
    return b


def _executor(client) -> ex.Executor:
    # dry_run=True so __init__ never builds a real client; inject the mock.
    e = ex.Executor(_settings(), dry_run=True)
    e._client = client
    e.telegram = RecordingTelegram()
    return e


def _exit_exec(client):
    o = type("Ex", (), {})()
    o.client = client
    o.dry_run = False
    o.telegram = RecordingTelegram()
    return o


def _em_setup(monkeypatch, tmp_path, ticker: str = "CL", qty: int = 23):
    monkeypatch.setattr(em, "WATCHERS_PATH", tmp_path / "w.json")
    (tmp_path / "w.json").write_text(json.dumps({
        "rows": {ticker: {"state": "AT_SL", "qty": qty, "sl": 80.0, "current": 79.5}}
    }))
    monkeypatch.setattr(em, "get_position_entry_price", lambda c, t: 84.0)
    monkeypatch.setattr(em, "get_bar_range", lambda c, t: (79.0, 80.5))
    monkeypatch.setattr("src.shadow.record_closed", lambda *a, **k: {})


# --------------------------------------------------------------------------- #
# 1-5: executor._process_sells
# --------------------------------------------------------------------------- #
def _run_sell(client, book):
    e = _executor(client)
    calls: list[tuple] = []
    with patch.object(ex, "submit_sell",
                      side_effect=lambda c, t, q: calls.append((t, q)) or "oid"), \
         patch.object(ex, "record_closed", lambda *a, **k: {}):
        e._process_sells(book, TODAY)
    return calls


def test_exec_live_short_skips():
    """(1) live short (qty_available < 0) -> no order."""
    client = MockClient([MockPos("CL", -23)])
    book = _book("CL", 23)
    assert _run_sell(client, book) == []
    assert len(book.positions) == 1


def test_exec_live_zero_skips():
    client = MockClient([MockPos("CL", 0)])
    book = _book("CL", 23)
    assert _run_sell(client, book) == []
    assert len(book.positions) == 1


def test_exec_api_error_skips_fail_closed():
    client = MockClient(error=True)
    book = _book("CL", 23)
    assert _run_sell(client, book) == []
    assert len(book.positions) == 1


def test_exec_live_less_than_book_sells_min():
    client = MockClient([MockPos("CL", 10)])
    book = _book("CL", 23)
    assert _run_sell(client, book) == [("CL", 10)]


def test_exec_stop_resting_qty_available_zero_skips():
    client = MockClient([MockPos("CL", 23, qty_available=0)])
    book = _book("CL", 23)
    assert _run_sell(client, book) == []
    assert len(book.positions) == 1


# --------------------------------------------------------------------------- #
# Same five, via exit_monitor.run_exit_monitor
# --------------------------------------------------------------------------- #
def _run_exit(client, book):
    sells: list[tuple] = []
    with patch.object(em, "submit_sell",
                      side_effect=lambda c, t, q: sells.append((t, q)) or "oid"):
        em.run_exit_monitor(_exit_exec(client), book)
    return sells


def test_exit_live_short_skips(monkeypatch, tmp_path):
    _em_setup(monkeypatch, tmp_path)
    client = MockClient([MockPos("CL", -23)])
    book = _book("CL", 23)
    assert _run_exit(client, book) == []
    assert len(book.positions) == 1


def test_exit_live_zero_skips(monkeypatch, tmp_path):
    _em_setup(monkeypatch, tmp_path)
    client = MockClient([MockPos("CL", 0)])
    book = _book("CL", 23)
    assert _run_exit(client, book) == []


def test_exit_api_error_skips(monkeypatch, tmp_path):
    _em_setup(monkeypatch, tmp_path)
    client = MockClient(error=True)
    book = _book("CL", 23)
    assert _run_exit(client, book) == []
    assert len(book.positions) == 1


def test_exit_live_less_than_book_sells_min(monkeypatch, tmp_path):
    _em_setup(monkeypatch, tmp_path)
    client = MockClient([MockPos("CL", 10)])
    book = _book("CL", 23)
    assert _run_exit(client, book) == [("CL", 10)]


def test_exit_stop_resting_qty_available_zero_skips(monkeypatch, tmp_path):
    _em_setup(monkeypatch, tmp_path)
    client = MockClient([MockPos("CL", 23, qty_available=0)])
    book = _book("CL", 23)
    assert _run_exit(client, book) == []
    assert len(book.positions) == 1


# --------------------------------------------------------------------------- #
# 6: CL/SCHW dry-run replay — the real incident shape
# --------------------------------------------------------------------------- #
def test_cl_schw_replay_zero_orders():
    """Book still holds CL 23 / SCHW 10 (stale), but the stops already
    flattened the account and live availability is 0. Pre-fix this shorts
    both; with the guard it must send ZERO orders and touch neither book row.
    """
    client = MockClient([
        MockPos("CL", 0, qty_available=0),
        MockPos("SCHW", 0, qty_available=0),
    ])
    book = Book()
    book.positions.append(Position(
        ticker="CL", qty=23, bot_id="candlestick-patterns",
        bot_name="Candlestick \u00b7 Pattern Finder",
        signal_id="candlestick-patterns-CL-13_BullEngulf-2026-09-28",
        enter_on="2026-09-28", close_on="2026-10-03",
        opened_at="2026-09-28T13:32:00+00:00"))
    book.positions.append(Position(
        ticker="SCHW", qty=10, bot_id="candlestick-patterns",
        bot_name="Candlestick \u00b7 Pattern Finder",
        signal_id="candlestick-patterns-SCHW-13_BullEngulf-2026-09-28",
        enter_on="2026-09-28", close_on="2026-10-03",
        opened_at="2026-09-28T13:33:00+00:00"))

    calls = _run_sell(client, book)
    print("CL/SCHW replay -> orders sent:", calls)
    assert calls == [], f"guard failed: sent {calls}"
    assert {p.ticker for p in book.positions} == {"CL", "SCHW"}, "book rows changed"
