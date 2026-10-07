"""Tests for the cancel-before-manual-sell guard (Step 2).

exit_monitor must clear that ticker's resting protective stop BEFORE the manual
sell, so a stop cannot double-fill. If the stop cannot be cancelled, abort.
cancel_open_orders() is NOT involved and stays market-only.
"""
from src.book import Book, Position
from src.exit_monitor import run_exit_monitor
import src.exit_monitor as em


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send(self, msg, dry_run=False):
        self.sent.append(msg)


class FakeStopOrder:
    def __init__(self, oid, otype="stop"):
        self.id = oid
        self.order_type = otype


class FakeOrder:
    def __init__(self, status, fill):
        self.status = status
        self.filled_avg_price = fill


class FakeClient:
    def __init__(self, stops=None, cancel_ok=True):
        self._stops = stops or []
        self._cancel_ok = cancel_ok
        self.cancelled = []

    def get_orders(self, filter):
        return list(self._stops)

    def cancel_order(self, order_id):
        if not self._cancel_ok:
            raise RuntimeError("cancel rejected")
        self.cancelled.append(order_id)

    def get_order_by_id(self, order_id):
        return FakeOrder("filled", 48.62)


def _book(ticker="EQT", qty=12):
    b = Book()
    b.positions.append(Position(
        ticker=ticker, qty=qty, bot_id="candlestick-patterns", bot_name="Candlestick · Pattern Finder",
        signal_id=f"candlestick-patterns-{ticker}-x-2026-09-28", enter_on="2026-09-28",
        close_on="2026-10-03", opened_at="2026-09-28T14:00:00+00:00"))
    return b


def _ex(client):
    ex = type("Ex", (), {})()
    ex.client = client
    ex.dry_run = False
    ex.telegram = FakeTelegram()
    return ex


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(em, "WATCHERS_PATH", tmp_path / "w.json")
    (tmp_path / "w.json").write_text(
        '{"rows": {"EQT": {"state": "AT_SL", "qty": 12, "sl": 48.68, "current": 48.645}}}')
    monkeypatch.setattr(em, "get_position_entry_price", lambda c, t: 50.09)
    monkeypatch.setattr(em, "get_bar_range", lambda c, t: (48.63, 48.67))
    monkeypatch.setattr("src.shadow.record_closed", lambda *a, **k: {})


def test_resting_stop_cancelled_before_sell(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    sells = []
    monkeypatch.setattr(em, "submit_sell", lambda c, t, q: sells.append((t, q)) or "oid")

    c = FakeClient(stops=[FakeStopOrder("stop-1")])
    run_exit_monitor(_ex(c), _book())
    assert c.cancelled == ["stop-1"]     # stop cleared first
    assert sells == [("EQT", 12)]        # then sold


def test_uncancellable_stop_aborts_sell(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    sells = []
    monkeypatch.setattr(em, "submit_sell", lambda c, t, q: sells.append(t) or "oid")

    c = FakeClient(stops=[FakeStopOrder("stop-9")], cancel_ok=False)
    book = _book()
    run_exit_monitor(_ex(c), book)
    assert sells == []                   # aborted
    assert len(book.positions) == 1      # still held


def test_no_stop_sells_normally(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    sells = []
    monkeypatch.setattr(em, "submit_sell", lambda c, t, q: sells.append(t) or "oid")

    c = FakeClient(stops=[])
    run_exit_monitor(_ex(c), _book())
    assert sells == ["EQT"]
