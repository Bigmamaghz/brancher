"""Replay + abort-path tests for the EQT 9/30 10:45 ET false-exit fix.

Ground truth (verified from Alpaca):
  EQT entry 50.09, frozen SL 48.68 (entry - ATR14 1.4093).
  2026-09-30 10:45 ET watcher row: current 48.645 -> AT_SL.
  1-min bar lows: 48.63 (IEX) and 48.63 (SIP) -> breach is REAL.
  The bad exit_px (50.30) came from a prior EQT sell fill, not the order placed.
"""
from src.book import Book, Position
from src.exit_monitor import run_exit_monitor, breach_confirmed
import src.exit_monitor as em


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send(self, msg, dry_run=False):
        self.sent.append(msg)


class FakeOrder:
    def __init__(self, status, filled_avg_price):
        self.status = status
        self.filled_avg_price = filled_avg_price


class FakeClient:
    """Returns a stale prior EQT sell fill for get_orders (the old bug path)."""

    def __init__(self, order_status="filled", fill=48.62):
        self._order_status = order_status
        self._fill = fill

    def get_orders(self, filter):
        return [FakeOrder("filled", 50.30)]  # a PRIOR sell — must NOT be used

    def get_order_by_id(self, order_id):
        return FakeOrder(self._order_status, self._fill)


def _book_with(ticker, qty=12):
    book = Book()
    book.positions.append(
        Position(
            ticker=ticker, qty=qty, bot_id="candlestick-patterns",
            bot_name="Candlestick · Pattern Finder",
            signal_id=f"candlestick-patterns-{ticker}-20_TweezerBottom-2026-09-28",
            enter_on="2026-09-28", close_on="2026-10-03",
            opened_at="2026-09-28T14:00:00+00:00",
        )
    )
    return book


def _ex(client):
    ex = type("Ex", (), {})()
    ex.client = client
    ex.dry_run = False
    ex.telegram = FakeTelegram()
    return ex


def test_breach_confirmed_logic():
    # AT_SL: bar low at/under stop confirms; above does not.
    assert breach_confirmed(48.63, 48.67, "AT_SL", {"sl": 48.68}) is True
    assert breach_confirmed(48.70, 48.75, "AT_SL", {"sl": 48.68}) is False
    # AT_TP: bar high at/over target confirms.
    assert breach_confirmed(51.9, 52.95, "AT_TP", {"tp": 52.91}) is True
    assert breach_confirmed(51.9, 52.50, "AT_TP", {"tp": 52.91}) is False
    # No bar reachable -> fall back to watcher's live price.
    assert breach_confirmed(None, None, "AT_SL", {"sl": 48.68, "current": 48.645}) is True
    assert breach_confirmed(None, None, "AT_SL", {"sl": 48.68, "current": 49.10}) is False


def test_eqt_replay_1015_tick_sells_and_uses_own_order(monkeypatch, tmp_path):
    """Replay the 9/30 10:45 ET tick: breach confirmed -> sells, exit_px from order id."""
    monkeypatch.setattr(em, "WATCHERS_PATH", tmp_path / "w.json")
    (tmp_path / "w.json").write_text(
        '{"rows": {"EQT": {"state": "AT_SL", "qty": 12, "sl": 48.68, "current": 48.645}}}'
    )
    monkeypatch.setattr(em, "get_position_entry_price", lambda c, t: 50.09)
    monkeypatch.setattr(em, "get_bar_range", lambda c, t: (48.63, 48.67))  # IEX/SIP low
    sells = []
    monkeypatch.setattr(em, "submit_sell", lambda c, t, q: sells.append((t, q)) or "oid-eqt")
    captured = {}
    monkeypatch.setattr(
        "src.shadow.record_closed",
        lambda book, t, *a, **k: (captured.update({"ticker": t, "args": a}), {})[1],
    )

    ex = _ex(FakeClient(order_status="filled", fill=48.62))
    book = _book_with("EQT", qty=12)
    run_exit_monitor(ex, book)

    assert sells == [("EQT", 12)]          # breach confirmed, sold at book qty
    assert len(book.positions) == 0
    assert len(ex.telegram.sent) == 1
    assert "stop-loss" in ex.telegram.sent[0]
    # exit_px came from the SUBMITTED order id (48.62), never the stale 50.30.
    assert captured["args"][5] == 48.62


def test_eqt_recovered_price_aborts(monkeypatch, tmp_path):
    """Same tick if the bar did NOT breach the stop -> no sell, position kept."""
    monkeypatch.setattr(em, "WATCHERS_PATH", tmp_path / "w.json")
    (tmp_path / "w.json").write_text(
        '{"rows": {"EQT": {"state": "AT_SL", "qty": 12, "sl": 48.68, "current": 48.70}}}'
    )
    monkeypatch.setattr(em, "get_position_entry_price", lambda c, t: 50.09)
    monkeypatch.setattr(em, "get_bar_range", lambda c, t: (48.70, 48.75))  # recovered
    sells = []
    monkeypatch.setattr(em, "submit_sell", lambda c, t, q: sells.append(t) or "oid")
    monkeypatch.setattr("src.shadow.record_closed", lambda *a, **k: {})

    ex = _ex(FakeClient())
    book = _book_with("EQT", qty=12)
    run_exit_monitor(ex, book)

    assert sells == []                     # abort path fired
    assert len(book.positions) == 1        # still held
    assert ex.telegram.sent == []
