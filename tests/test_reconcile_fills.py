"""Book a close only from a confirmed fill, and reconcile stops Alpaca already filled.

No network. Nothing here submits, cancels, or covers a short.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import src.decision_log as dl
import src.executor as ex
import src.exit_monitor as em
from src.book import Book, Position
from src.config import Settings
from src.reconcile import BrokerFill, reconcile_book, select_closing_fill

STOP_TIME = "2026-10-02T19:58:00+00:00"
SHORT_TIME = "2026-10-05T13:31:00+00:00"


@pytest.fixture(autouse=True)
def _isolate_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "LOG_DIR", tmp_path)
    monkeypatch.setattr(dl, "LOG_PATH", tmp_path / "decisions.csv")
    monkeypatch.setattr("src.shadow.SHADOW_PATH", tmp_path / "shadow.jsonl")
    yield


class Pos:
    def __init__(self, symbol, qty, qty_available=None, side=None):
        self.symbol = symbol
        self.qty = qty
        self.qty_available = qty if qty_available is None else qty_available
        self.side = side
        self.avg_entry_price = 84.0


class Order:
    def __init__(self, oid, price, filled_at, qty, status="filled", side="sell"):
        self.id = oid
        self.status = status
        self.side = side
        self.symbol = None
        self.filled_avg_price = price
        self.filled_at = filled_at
        self.filled_qty = qty
        self.qty = qty


class Client:
    def __init__(self, positions=None, orders=None, by_id=None, boom=False, market_open=True):
        self._positions = positions or []
        self._orders = orders or []
        self._by_id = by_id or {}
        self._boom = boom
        self._market_open = market_open
        self.sells = []
        self.buys = []
        self.cancelled = []

    def get_all_positions(self):
        if self._boom:
            raise RuntimeError("alpaca down")
        return list(self._positions)

    def get_orders(self, filter=None):
        return list(self._orders)

    def get_order_by_id(self, oid):
        if oid not in self._by_id:
            raise RuntimeError(f"missing {oid}")
        return self._by_id[oid]

    def get_clock(self):
        class C:
            is_open = self._market_open
        return C()

    def submit_order(self, req):
        raise AssertionError("reconcile tests must not submit")


class Telegram:
    def __init__(self):
        self.sent = []

    def send(self, text, dry_run=False):
        self.sent.append(text)
        return True


def _settings() -> Settings:
    return Settings(
        alpaca_api_key="", alpaca_secret_key="", alpaca_base_url=None,
        telegram_bot_token="", telegram_chat_id="",
        paper_qty=1, paper_qty_max_mult=1,
        max_open_positions=10, max_opens_per_day=5, min_hit=0.75, min_n=30,
        telegram_news_only=True, telegram_eod=False, telegram_trades_only=False,
        joe_veto=False,
    )


def _pos(ticker, qty, close_on="2026-10-03") -> Position:
    return Position(
        ticker=ticker, qty=qty, bot_id="candlestick-patterns",
        bot_name="Candlestick · Pattern Finder",
        signal_id=f"candlestick-patterns-{ticker}-x-2026-09-28",
        enter_on="2026-09-28", close_on=close_on,
        opened_at="2026-09-28T13:32:00+00:00",
        stop=80.0, target=90.0,
    )


def _book(*positions) -> Book:
    book = Book()
    book.positions.extend(positions)
    return book


def _executor(client) -> ex.Executor:
    exe = ex.Executor(_settings(), dry_run=True)
    exe._client = client
    exe.telegram = Telegram()
    return exe


def test_stop_already_filled_books_fill_and_does_not_sell():
    """A stop that flattened the name is booked at its own price and time. No new sell."""
    stop = Order("stop-cl", 84.20, STOP_TIME, 23)
    client = Client(positions=[Pos("CL", 0, qty_available=0)], orders=[stop])
    book = _book(_pos("CL", 23))
    exe = _executor(client)
    sells = []
    with patch.object(ex, "submit_sell", side_effect=lambda c, t, q: sells.append((t, q)) or "nope"):
        exe._announce_reconcile(book)
        exe._process_sells(book, "2026-10-07")
    assert sells == []
    assert book.positions == []
    assert book.closed[0]["exit_px"] == 84.20
    assert book.closed[0]["closed_at"] == STOP_TIME
    assert any("84.2" in msg for msg in exe.telegram.sent)


def test_resting_stop_qty_available_zero_does_not_reconcile_close():
    """qty_available 0 with qty still long is a resting stop, not a closed trade."""
    tempting = Order("old-sell", 84.20, STOP_TIME, 23)
    client = Client(
        positions=[Pos("CL", 23, qty_available=0)],
        orders=[tempting],
    )
    book = _book(_pos("CL", 23))
    events = reconcile_book(client, book)
    assert events == []
    assert len(book.positions) == 1
    assert book.closed == []


def test_flat_without_a_fill_is_not_guessed_closed():
    client = Client(positions=[Pos("CL", 0), Pos("SCHW", 0)])
    book = _book(_pos("CL", 23), _pos("SCHW", 10))
    assert reconcile_book(client, book) == []
    assert {p.ticker for p in book.positions} == {"CL", "SCHW"}
    assert book.closed == []


def test_live_sell_books_only_its_own_fill_time():
    filled = Order("oid-1", 84.20, STOP_TIME, 23)
    client = Client(
        positions=[Pos("CL", 23)],
        by_id={"oid-1": filled},
    )
    book = _book(_pos("CL", 23))
    exe = _executor(client)
    with patch.object(ex, "submit_sell", return_value="oid-1"):
        exe._process_sells(book, "2026-10-07")
    assert book.positions == []
    assert book.closed[0]["exit_px"] == 84.20
    assert book.closed[0]["closed_at"] == STOP_TIME


def test_unfilled_sell_stays_open_and_is_not_sent_again():
    working = Order("oid-1", None, None, 23, status="accepted")
    client = Client(positions=[Pos("CL", 23)], by_id={"oid-1": working})
    book = _book(_pos("CL", 23))
    exe = _executor(client)
    calls = []
    with patch.object(ex, "submit_sell", side_effect=lambda c, t, q: calls.append(1) or "oid-1"):
        exe._process_sells(book, "2026-10-07")
        exe._process_sells(book, "2026-10-07")
    assert calls == [1]
    assert len(book.positions) == 1
    assert book.positions[0].pending_exit_order_id == "oid-1"
    assert book.closed == []


def test_pending_fill_books_even_when_the_market_is_closed():
    filled = Order("oid-1", 96.57, STOP_TIME, 10)
    client = Client(
        positions=[Pos("SCHW", 10)],
        by_id={"oid-1": filled},
        market_open=False,
    )
    pos = _pos("SCHW", 10)
    pos.pending_exit_order_id = "oid-1"
    pos.entry_px = 100.0
    book = _book(pos)
    exe = _executor(client)
    with patch.object(ex, "submit_sell", side_effect=AssertionError("must not sell")):
        exe._process_sells(book, "2026-10-07")
    assert book.positions == []
    assert book.closed[0]["exit_px"] == 96.57
    assert book.closed[0]["closed_at"] == STOP_TIME


def test_skip_guard_does_not_book_some_other_closed_sell():
    """The sell path must not copy the newest closed sell into the book."""
    other = Order("old-stop", 84.20, STOP_TIME, 23)
    client = Client(positions=[Pos("CL", 0, qty_available=0)], orders=[other], by_id={})
    book = _book(_pos("CL", 23))
    exe = _executor(client)
    with patch.object(ex, "submit_sell", side_effect=AssertionError("must not sell")):
        exe._process_sells(book, "2026-10-07")
    assert len(book.positions) == 1
    assert book.closed == []


def test_later_short_open_is_not_the_long_exit():
    stop = Order("stop-cl", 84.20, STOP_TIME, 23)
    short_open = Order("short-cl", 84.80, SHORT_TIME, 23)
    client = Client(positions=[Pos("CL", -23)], orders=[short_open, stop])
    book = _book(_pos("CL", 23))
    events = reconcile_book(client, book)
    assert book.positions == []
    assert book.closed[0]["exit_px"] == 84.20
    assert book.closed[0]["closed_at"] == STOP_TIME
    assert any(event.kind == "unexpected_short" and event.qty == -23 for event in events)
    assert all(event.kind != "unexpected_long" for event in events)


def test_select_closing_fill_skips_sells_that_explain_the_short():
    stop = BrokerFill("stop", 84.20, STOP_TIME, 23)
    opened = BrokerFill("short", 84.80, SHORT_TIME, 23)
    assert select_closing_fill([opened, stop], -23).order_id == "stop"
    assert select_closing_fill([opened], -23) is None
    assert select_closing_fill([stop], 23) is None


def test_unexpected_short_is_not_added_or_covered_and_alerts_once():
    client = Client(positions=[Pos("CL", -23), Pos("SCHW", -10)])
    book = Book()
    exe = _executor(client)
    exe._announce_reconcile(book)
    first = list(exe.telegram.sent)
    exe._announce_reconcile(book)
    assert book.positions == []
    assert book.closed == []
    assert client.buys == []
    assert client.sells == []
    assert len(first) == 2
    assert all("UNEXPECTED SHORT" in msg for msg in first)
    assert "CL" in first[0] or "CL" in first[1]
    assert "SCHW" in first[0] or "SCHW" in first[1]
    assert all("not trading" in msg.lower() or "Not adding" in msg for msg in first)
    assert exe.telegram.sent == first


def test_api_error_leaves_the_book_unchanged():
    client = Client(boom=True)
    book = _book(_pos("CL", 23))
    assert reconcile_book(client, book) == []
    assert len(book.positions) == 1
    assert book.closed == []


def test_from_dict_ignores_unknown_keys_and_defaults_pending():
    raw = _pos("DTE", 5).to_dict()
    raw["legacy_note"] = "ignore me"
    loaded = Position.from_dict(raw)
    assert loaded.ticker == "DTE"
    assert loaded.pending_exit_order_id is None
    assert loaded.entry_px is None


def test_run_cycle_reconciles_before_sells(monkeypatch):
    book = Book()
    seen = {}

    def fake_reconcile(client, got):
        seen["client"] = client
        seen["book"] = got
        return []

    monkeypatch.setattr(ex, "reconcile_book", fake_reconcile)
    monkeypatch.setattr(ex, "poll_all", lambda bots: [])
    monkeypatch.setattr(ex, "detect_news", lambda *a, **k: ([], {}, {}))
    monkeypatch.setattr(ex, "merge_signals", lambda results: ([], []))
    monkeypatch.setattr(ex, "load_book", lambda: book)
    monkeypatch.setattr(ex, "save_book", lambda b: None)
    monkeypatch.setattr(ex, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(ex, "run_exit_monitor", lambda *a, **k: None)
    client = Client()
    exe = _executor(client)
    exe.run_cycle(bots=[object()])
    assert seen["client"] is client
    assert seen["book"] is book


def test_working_fast_exit_does_not_cancel_stop_or_sell_again(monkeypatch, tmp_path):
    monkeypatch.setattr(em, "WATCHERS_PATH", tmp_path / "w.json")
    (tmp_path / "w.json").write_text(
        '{"rows": {"CL": {"state": "AT_SL", "qty": 23, "sl": 80.0, "current": 79.0}}}'
    )
    working = Order("oid-fast", None, None, 23, status="accepted")
    client = Client(
        positions=[Pos("CL", 23)],
        orders=[type("Stop", (), {"id": "prot-stop", "order_type": "stop"})()],
        by_id={"oid-fast": working},
    )
    pos = _pos("CL", 23)
    pos.pending_exit_order_id = "oid-fast"
    book = _book(pos)
    holder = type("H", (), {})()
    holder.client = client
    holder.dry_run = False
    holder.telegram = Telegram()
    sells = []
    monkeypatch.setattr(em, "submit_sell", lambda c, t, q: sells.append(t) or "new")
    monkeypatch.setattr(em, "get_bar_range", lambda c, t: (79.0, 80.0))
    em.run_exit_monitor(holder, book)
    assert sells == []
    assert client.cancelled == []
    assert book.positions[0].pending_exit_order_id == "oid-fast"
