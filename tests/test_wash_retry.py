"""Mocked-Alpaca tests for the wash-trade (40310000) retry guard.

ZERO real orders: every Alpaca interaction is a mock. Covers BOTH:
  * buy side  -> src.executor.Executor._process_enters
  * sell side -> src.exit_monitor.run_exit_monitor

Assertions (per request):
  (1) a 40310000 / "wash trade" reject triggers exactly ONE retry
  (2) the retry never cancels a protective STOP order sitting on the same symbol
  (3) a second reject does not loop (no further retries)

The retry calls the REAL src.alpaca.cancel_open_orders (not a stub), so this
test reflects production behaviour: it cancels every OPEN order for the symbol.

Runnable two ways:
  pytest tests/test_wash_retry.py
  /Users/mybot/joe/trainvenv/bin/python tests/test_wash_retry.py
"""
from __future__ import annotations

from unittest.mock import patch

import src.executor as ex
import src.exit_monitor as em
from src.book import Book, Position
from src.config import Settings
from src.merge import MergedSignal
from src.poll import Signal

TODAY = "2026-09-29"


# --------------------------------------------------------------------------- #
# Mock Alpaca surface
# --------------------------------------------------------------------------- #
class MockOrder:
    """Minimal order object; real cancel_open_orders only reads .id."""

    def __init__(self, oid: str, side: str, order_type: str):
        self.id = oid
        self.side = side
        self.order_type = order_type  # "market" | "stop" | "limit"


class MockClient:
    """Tracks open orders. cancel_order() records + removes them (like Alpaca)."""

    def __init__(self, orders: list[MockOrder]):
        self.open = {o.id: o for o in orders}
        self.cancelled: list[str] = []

    def get_orders(self, filter=None):
        # Real code queries status=OPEN for one symbol; all here share the symbol.
        return list(self.open.values())

    def get_clock(self):
        # Market-hours gate reads this; model an OPEN market.
        class C:
            is_open = True
        return C()

    def get_all_positions(self):
        # Live-quantity guard reads this; model an open long for the test ticker.
        class P:
            def __init__(self, s, q):
                self.symbol = s
                self.qty = q
                self.qty_available = q
                self.avg_entry_price = 100.0
        return [P("ZZZ", 10)]

    def cancel_order(self, oid):
        if oid in self.open:
            self.cancelled.append(oid)
            del self.open[oid]

    def get_order_by_id(self, oid):
        class O:
            status = "filled"
            filled_avg_price = 10.0
            filled_at = "2026-09-29T15:00:00+00:00"
            filled_qty = 10
            side = "sell"
            id = oid
        return O()

    def still_open(self, oid: str) -> bool:
        return oid in self.open


class RecordingTelegram:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, text, dry_run=False):
        self.sent.append(text)
        return True


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
def _settings(max_mult: int = 1) -> Settings:
    return Settings(
        alpaca_api_key="", alpaca_secret_key="", alpaca_base_url=None,
        telegram_bot_token="", telegram_chat_id="",
        paper_qty=1, paper_qty_max_mult=max_mult,
        max_open_positions=10, max_opens_per_day=5, min_hit=0.75, min_n=30,
        telegram_news_only=True, telegram_eod=False, telegram_trades_only=False,
        joe_veto=False,
    )


def _signal(ticker="ZZZ") -> Signal:
    return Signal(
        id=f"fleet-wave1-{ticker}-test", ticker=ticker, side="UP", hit=0.80, n=30,
        event_type="test", enter_on=TODAY, close_on="2026-10-03", eligible=True,
        urgency="normal", sent_at="2026-09-29T00:00:00+00:00",
        net_r_full=0.2, net_r_recent=0.2,
    )


def _merged(sig: Signal) -> MergedSignal:
    return MergedSignal(signal=sig, bot_id="fleet-wave1", bot_name="Fleet · Wave1 Confirmed")


def _executor(settings: Settings, client: MockClient) -> ex.Executor:
    # dry_run=True so __init__ never builds a real client; inject the mock.
    e = ex.Executor(settings, dry_run=True)
    e._client = client
    e.telegram = RecordingTelegram()
    return e


def _wash_reject():
    return Exception(
        '{"code":40310000,"message":"potential wash trade detected. use complex orders"}'
    )


# --------------------------------------------------------------------------- #
# BUY SIDE — src.executor
# --------------------------------------------------------------------------- #
def test_buy_retries_exactly_once_and_opens_position():
    """(1) one wash reject -> exactly one retry -> position opened."""
    client = MockClient([MockOrder("stale-sell-1", "sell", "market")])
    exe = _executor(_settings(), client)
    book, sig = Book(), _signal()
    calls = {"n": 0}

    def fake_buy(c, t, q):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _wash_reject()
        return "buy-order-2"

    with patch.object(ex, "submit_buy", side_effect=fake_buy):
        exe._process_enters([_merged(sig)], book, TODAY)

    assert calls["n"] == 2, f"expected exactly 2 buy attempts, got {calls['n']}"
    assert len(book.positions) == 1 and book.positions[0].ticker == "ZZZ"
    assert "stale-sell-1" in client.cancelled


def test_buy_retry_does_not_cancel_protective_stop():
    """(2) PROTECTIVE STOP on the same symbol must survive the cancel-retry."""
    client = MockClient([
        MockOrder("protected-stop-1", "sell", "stop"),   # resting protective stop
        MockOrder("stale-sell-1", "sell", "market"),      # stale opposite-side order
    ])
    exe = _executor(_settings(), client)
    book, sig = Book(), _signal()
    calls = {"n": 0}

    def fake_buy(c, t, q):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _wash_reject()
        return "buy-order-2"

    with patch.object(ex, "submit_buy", side_effect=fake_buy):
        exe._process_enters([_merged(sig)], book, TODAY)

    assert calls["n"] == 2
    assert client.still_open("protected-stop-1"), (
        "PROTECTIVE STOP WAS CANCELLED by cancel_open_orders: "
        f"cancelled={client.cancelled}"
    )


def test_buy_second_reject_does_not_loop():
    """(3) two rejects -> no more than 2 attempts (initial + one retry)."""
    client = MockClient([MockOrder("stale-sell-1", "sell", "market")])
    exe = _executor(_settings(), client)
    book, sig = Book(), _signal()
    calls = {"n": 0}

    def always_reject(c, t, q):
        calls["n"] += 1
        raise _wash_reject()

    with patch.object(ex, "submit_buy", side_effect=always_reject):
        exe._process_enters([_merged(sig)], book, TODAY)

    assert calls["n"] == 2, f"expected 2 attempts (no loop), got {calls['n']}"
    assert len(book.positions) == 0


def test_buy_no_retry_when_nothing_to_cancel():
    """Retry only fires with a cancellable order; otherwise a single attempt."""
    client = MockClient([])  # no open orders
    exe = _executor(_settings(), client)
    book, sig = Book(), _signal()
    calls = {"n": 0}

    def reject(c, t, q):
        calls["n"] += 1
        raise _wash_reject()

    with patch.object(ex, "submit_buy", side_effect=reject):
        exe._process_enters([_merged(sig)], book, TODAY)

    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# SELL SIDE — src.exit_monitor
# --------------------------------------------------------------------------- #
def _book_with(ticker: str, qty: int = 10) -> Book:
    book = Book()
    book.positions.append(Position(
        ticker=ticker, qty=qty, bot_id="fleet-wave1", bot_name="Fleet · Wave1 Confirmed",
        signal_id=f"fleet-wave1-{ticker}-test", enter_on=TODAY, close_on="2026-10-03",
        opened_at="2026-09-29T14:00:00+00:00",
    ))
    return book


def _ex_holder(client: MockClient):
    h = type("H", (), {})()
    h.client = client
    h.dry_run = True
    h.telegram = RecordingTelegram()
    return h


def _write_at_sl(tmp_path):
    p = tmp_path / "w.json"
    p.write_text('{"rows": {"ZZZ": {"state": "AT_SL", "qty": 10, "sl": 9.5}}}')
    return p


def test_sell_retries_exactly_once(tmp_path):
    """(1) sell-side: one wash reject -> exactly one retry."""
    client = MockClient([MockOrder("stale-buy-1", "buy", "market")])
    holder = _ex_holder(client)
    book = _book_with("ZZZ")
    calls = {"n": 0}

    def fake_sell(c, t, q):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _wash_reject()
        return "sell-order-2"

    with patch.object(em, "WATCHERS_PATH", _write_at_sl(tmp_path)), \
         patch.object(em, "get_position_entry_price", lambda c, t: 10.0), \
         patch.object(em, "submit_sell", side_effect=fake_sell), \
         patch("src.shadow.record_closed", lambda *a, **k: {}):
        em.run_exit_monitor(holder, book)

    assert calls["n"] == 2, f"expected exactly 2 sell attempts, got {calls['n']}"
    assert len(book.positions) == 0
    assert "stale-buy-1" in client.cancelled


def test_sell_retry_does_not_cancel_protective_stop(tmp_path):
    """(2) sell-side: a resting protective stop must survive the retry."""
    client = MockClient([
        MockOrder("protected-stop-1", "sell", "stop"),
        MockOrder("stale-buy-1", "buy", "market"),
    ])
    holder = _ex_holder(client)
    book = _book_with("ZZZ")

    def fake_sell(c, t, q):
        raise _wash_reject()

    with patch.object(em, "WATCHERS_PATH", _write_at_sl(tmp_path)), \
         patch.object(em, "get_position_entry_price", lambda c, t: 10.0), \
         patch.object(em, "submit_sell", side_effect=fake_sell), \
         patch("src.shadow.record_closed", lambda *a, **k: {}):
        em.run_exit_monitor(holder, book)

    assert client.still_open("protected-stop-1"), (
        "PROTECTIVE STOP WAS CANCELLED by cancel_open_orders: "
        f"cancelled={client.cancelled}"
    )


def test_sell_second_reject_does_not_loop(tmp_path):
    """(3) sell-side: repeat rejects -> no loop."""
    client = MockClient([MockOrder("stale-buy-1", "buy", "market")])
    holder = _ex_holder(client)
    book = _book_with("ZZZ")
    calls = {"n": 0}

    def always_reject(c, t, q):
        calls["n"] += 1
        raise _wash_reject()

    with patch.object(em, "WATCHERS_PATH", _write_at_sl(tmp_path)), \
         patch.object(em, "get_position_entry_price", lambda c, t: 10.0), \
         patch.object(em, "submit_sell", side_effect=always_reject), \
         patch("src.shadow.record_closed", lambda *a, **k: {}):
        em.run_exit_monitor(holder, book)

    assert calls["n"] == 2, f"expected 2 attempts (no loop), got {calls['n']}"


# --------------------------------------------------------------------------- #
# Standalone runner (no pytest needed)
# --------------------------------------------------------------------------- #
def _run_standalone():
    import tempfile, traceback
    from pathlib import Path

    results = []
    # tmp_path fixture replacement
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tests = [
            ("test_buy_retries_exactly_once_and_opens_position", lambda: test_buy_retries_exactly_once_and_opens_position()),
            ("test_buy_retry_does_not_cancel_protective_stop", lambda: test_buy_retry_does_not_cancel_protective_stop()),
            ("test_buy_second_reject_does_not_loop", lambda: test_buy_second_reject_does_not_loop()),
            ("test_buy_no_retry_when_nothing_to_cancel", lambda: test_buy_no_retry_when_nothing_to_cancel()),
            ("test_sell_retries_exactly_once", lambda: test_sell_retries_exactly_once(tmp)),
            ("test_sell_retry_does_not_cancel_protective_stop", lambda: test_sell_retry_does_not_cancel_protective_stop(tmp)),
            ("test_sell_second_reject_does_not_loop", lambda: test_sell_second_reject_does_not_loop(tmp)),
        ]
        for name, fn in tests:
            try:
                fn()
                results.append((name, "PASS", ""))
            except AssertionError as e:
                results.append((name, "FAIL", str(e)))
            except Exception as e:
                results.append((name, "ERROR", f"{type(e).__name__}: {e}\n" + traceback.format_exc()))

    print("=" * 72)
    print("wash-trade retry — mocked Alpaca, zero real orders")
    print("=" * 72)
    for name, status, detail in results:
        print(f"[{status}] {name}")
        if detail:
            for line in detail.rstrip().splitlines():
                print(f"        {line}")
    npass = sum(1 for _, s, _ in results if s == "PASS")
    print("-" * 72)
    print(f"{npass}/{len(results)} passed")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
