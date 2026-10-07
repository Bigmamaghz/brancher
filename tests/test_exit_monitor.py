from src.book import Book, Position
from src.exit_monitor import run_exit_monitor
import src.exit_monitor as em
from pathlib import Path


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send(self, msg, dry_run=False):
        self.sent.append(msg)


class FakeClient:
    def __init__(self, avail: int = 10):
        self._avail = avail

    def get_orders(self, filter):
        return []

    def get_clock(self):
        # Market-hours gate reads this; model an OPEN market.
        class C:
            is_open = True
        return C()

    def get_all_positions(self):
        # Live-quantity guard reads this; model a long position for each ticker.
        class P:
            def __init__(self, s, q):
                self.symbol = s
                self.qty = q
                self.qty_available = q
                self.avg_entry_price = 10.0
        return [P(t, self._avail) for t in ("AAA", "BBB", "CCC")]

    def get_order_by_id(self, order_id):
        class O:
            status = "filled"
            filled_avg_price = 10.0
            filled_at = "2026-09-30T15:00:00+00:00"
            filled_qty = 10
            side = "sell"
            id = order_id
        return O()


def _book_with(ticker, qty=10):
    book = Book()
    book.positions.append(
        Position(
            ticker=ticker, qty=qty, bot_id="financials-xlf", bot_name="Financials · XLF",
            signal_id=f"financials-xlf-{ticker}-test", enter_on="2026-09-29", close_on="2026-10-03",
            opened_at="2026-09-29T14:00:00+00:00",
        )
    )
    return book


def _ex():
    ex = type("Ex", (), {})()
    ex.client = FakeClient()
    ex.dry_run = False
    ex.telegram = FakeTelegram()
    return ex


def test_at_sl_sells(monkeypatch, tmp_path):
    monkeypatch.setattr(em, "WATCHERS_PATH", tmp_path / "w.json")
    (tmp_path / "w.json").write_text('{"rows": {"AAA": {"state": "AT_SL", "qty": 10, "sl": 9.5}}}')
    monkeypatch.setattr(em, "get_position_entry_price", lambda c, t: 10.0)
    sells = []
    monkeypatch.setattr(em, "submit_sell", lambda c, t, q: sells.append((t, q)) or "oid-1")
    monkeypatch.setattr("src.shadow.record_closed", lambda *a, **k: {})

    ex = _ex()
    book = _book_with("AAA")
    run_exit_monitor(ex, book)
    assert sells == [("AAA", 10)]
    assert len(book.positions) == 0
    assert len(ex.telegram.sent) == 1
    assert "stop-loss" in ex.telegram.sent[0]


def test_at_tp_sells_and_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(em, "WATCHERS_PATH", tmp_path / "w.json")
    (tmp_path / "w.json").write_text('{"rows": {"AAA": {"state": "AT_TP", "qty": 10, "tp": 12.0}}}')
    monkeypatch.setattr(em, "get_position_entry_price", lambda c, t: 10.0)
    sells = []
    monkeypatch.setattr(em, "submit_sell", lambda c, t, q: sells.append(t) or "oid-2")
    monkeypatch.setattr("src.shadow.record_closed", lambda *a, **k: {})

    ex = _ex()
    book = _book_with("AAA")
    run_exit_monitor(ex, book)
    assert sells == ["AAA"]
    assert len(book.positions) == 0
    assert len(ex.telegram.sent) == 1
    assert "take-profit" in ex.telegram.sent[0]


def test_ignores_near_and_not_on_book(monkeypatch, tmp_path):
    monkeypatch.setattr(em, "WATCHERS_PATH", tmp_path / "w.json")
    (tmp_path / "w.json").write_text(
        '{"rows": {"BBB": {"state": "NEAR_SL", "qty": 10}, "CCC": {"state": "AT_TP", "qty": 10}}}'
    )
    monkeypatch.setattr(em, "get_position_entry_price", lambda c, t: 10.0)
    sells = []
    monkeypatch.setattr(em, "submit_sell", lambda c, t, q: sells.append(t) or "oid")
    monkeypatch.setattr("src.shadow.record_closed", lambda *a, **k: {})

    book = _book_with("CCC")  # BBB not on book
    run_exit_monitor(_ex(), book)
    assert sells == ["CCC"]
    assert len(book.positions) == 0


def test_qty_mismatch_skips(monkeypatch, tmp_path):
    monkeypatch.setattr(em, "WATCHERS_PATH", tmp_path / "w.json")
    (tmp_path / "w.json").write_text('{"rows": {"AAA": {"state": "AT_SL", "qty": 99}}}')
    sells = []
    monkeypatch.setattr(em, "submit_sell", lambda c, t, q: sells.append(t) or "oid")
    monkeypatch.setattr("src.shadow.record_closed", lambda *a, **k: {})

    book = _book_with("AAA", qty=10)
    run_exit_monitor(_ex(), book)
    assert sells == []
    assert len(book.positions) == 1
