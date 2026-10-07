"""Exit sells must not open shorts, and closes book only on a real fill."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from src.alpaca import PositionIntent, signed_position_qty
from src.book import Book, Position
from src.config import Settings
from src.executor import Executor
from src.exits import BrokerFill, select_closing_fill

STOP_TIME = "2026-10-03T18:01:00+00:00"
SHORT_TIME = "2026-10-06T13:30:05+00:00"
FILL_TIME = "2026-10-06T14:32:01+00:00"
TODAY = "2026-10-07"

_OPEN = {
    "new",
    "accepted",
    "pending_new",
    "partially_filled",
    "pending_cancel",
    "pending_replace",
    "stopped",
}


def _settings() -> Settings:
    return Settings(
        alpaca_api_key="",
        alpaca_secret_key="",
        alpaca_base_url=None,
        telegram_bot_token="",
        telegram_chat_id="",
        paper_qty=1,
        paper_qty_max_mult=4,
        max_open_positions=10,
        max_opens_per_day=5,
        min_hit=0.75,
        telegram_news_only=True,
        telegram_eod=False,
        telegram_trades_only=False,
        joe_veto=False,
    )


def _position(ticker: str, qty: int, close_on: str = "2026-10-01") -> Position:
    return Position(
        ticker=ticker,
        qty=qty,
        bot_id="author",
        bot_name="Author",
        signal_id=f"{ticker}:test:2026-09-01",
        enter_on="2026-09-01",
        close_on=close_on,
        opened_at="2026-09-01T14:30:00+00:00",
    )


def _side(value: str) -> SimpleNamespace:
    return SimpleNamespace(value=value)


def _broker_position(symbol: str, qty: int, side: str = "long", avg: str = "100") -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        qty=str(abs(qty)),
        side=_side(side),
        avg_entry_price=avg,
    )


def _order(
    order_id: str,
    symbol: str,
    *,
    status: str,
    qty: int,
    side: str = "sell",
    price: str | None = None,
    filled_at: str | None = None,
    filled_qty: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=order_id,
        symbol=symbol,
        status=status,
        qty=str(qty),
        side=_side(side),
        filled_avg_price=price,
        filled_at=filled_at,
        filled_qty=filled_qty if filled_qty is not None else ("0" if status != "filled" else str(qty)),
    )


class FakeAlpaca:
    """In-memory Alpaca trading client. No network."""

    def __init__(self) -> None:
        self.positions: list = []
        self.orders: list = []
        self.activities: list = []
        self.submitted: list = []
        self.cancelled: list[str] = []
        self.fill_on_submit: tuple[str, str] | None = None

    def get_all_positions(self):
        return list(self.positions)

    def get_orders(self, filter=None):
        status = ""
        symbols: set[str] = set()
        side = ""
        if filter is not None:
            status = str(getattr(getattr(filter, "status", None), "value", "") or "")
            symbols = set(getattr(filter, "symbols", None) or [])
            side_obj = getattr(filter, "side", None)
            side = str(getattr(side_obj, "value", side_obj) or "")
        matched = []
        for order in self.orders:
            if symbols and order.symbol not in symbols:
                continue
            if side and order.side.value != side:
                continue
            is_open = order.status in _OPEN
            if status == "open" and not is_open:
                continue
            if status == "closed" and is_open:
                continue
            matched.append(order)
        return matched

    def cancel_order_by_id(self, order_id) -> None:
        self.cancelled.append(str(order_id))
        for order in self.orders:
            if str(order.id) == str(order_id):
                order.status = "canceled"

    def submit_order(self, request):
        self.submitted.append(request)
        if self.fill_on_submit:
            price, filled_at = self.fill_on_submit
            status = "filled"
            filled_qty = str(int(request.qty))
            self.positions = [p for p in self.positions if p.symbol != request.symbol]
        else:
            price, filled_at = None, None
            status = "accepted"
            filled_qty = "0"
        order = _order(
            f"submitted-{len(self.submitted)}",
            request.symbol,
            status=status,
            qty=int(request.qty),
            side="sell",
            price=price,
            filled_at=filled_at,
            filled_qty=filled_qty,
        )
        # Keep the enum side from the request so later filters see a real sell.
        order.side = request.side
        self.orders.append(order)
        return order

    def get_order_by_id(self, order_id):
        for order in self.orders:
            if str(order.id) == str(order_id):
                return order
        raise AssertionError(f"unknown order {order_id}")

    def get_fill_activities(self):
        return list(self.activities)


@pytest.fixture
def shadow_path(tmp_path, monkeypatch):
    path = tmp_path / "shadow.jsonl"
    monkeypatch.setattr("src.shadow.SHADOW_PATH", path)
    return path


@pytest.fixture
def executor():
    fake = FakeAlpaca()
    bot = Executor(_settings(), dry_run=True)
    bot._client = fake
    return bot, fake


def _run_exit(bot: Executor, book: Book) -> None:
    bot.reconcile_book(book)
    bot._process_sells(book, TODAY)


def test_stop_already_filled_then_timed_exit_does_not_sell(executor, shadow_path, caplog):
    """The October incident: stop already filled, then the date exit comes due."""
    caplog.set_level(logging.INFO)
    bot, fake = executor
    fake.orders.append(
        _order("stop-cl", "CL", status="filled", qty=23, price="84.20", filled_at=STOP_TIME)
    )
    # The old bot's market sell is still resting. It must be cancelled, not joined.
    fake.orders.append(_order("bad-exit", "CL", status="accepted", qty=23))
    book = Book(positions=[_position("CL", 23, close_on="2026-10-06")])

    bot._process_sells(book, TODAY)

    assert fake.submitted == []
    assert "bad-exit" in fake.cancelled
    assert book.positions == []
    assert len(book.closed) == 1
    assert book.closed[0]["exit_px"] == 84.20
    assert book.closed[0]["closed_at"] == STOP_TIME
    assert "Skipping exit sell for CL" in caplog.text
    assert "no long position" in caplog.text


def test_exit_sell_capped_to_held_qty(executor, shadow_path, caplog):
    caplog.set_level(logging.INFO)
    bot, fake = executor
    fake.positions.append(_broker_position("SCHW", 4, avg="90"))
    book = Book(positions=[_position("SCHW", 10)])

    _run_exit(bot, book)

    assert len(fake.submitted) == 1
    assert int(fake.submitted[0].qty) == 4
    assert fake.submitted[0].position_intent == PositionIntent.SELL_TO_CLOSE
    assert book.closed == []
    assert [p.ticker for p in book.positions] == ["SCHW"]
    assert book.positions[0].pending_exit_order_id
    assert "Capping exit sell for SCHW from 10 to 4" in caplog.text


def test_close_booked_only_after_fill(executor, shadow_path, caplog):
    caplog.set_level(logging.INFO)
    bot, fake = executor
    fake.positions.append(_broker_position("DTE", 5, avg="70"))
    book = Book(positions=[_position("DTE", 5)])

    _run_exit(bot, book)

    assert len(fake.submitted) == 1
    assert book.closed == []
    assert book.positions[0].pending_exit_order_id == "submitted-1"
    assert "Book stays open until Alpaca confirms the fill" in caplog.text

    order = fake.get_order_by_id("submitted-1")
    order.status = "filled"
    order.filled_avg_price = "123.45"
    order.filled_at = FILL_TIME
    order.filled_qty = "5"
    fake.positions.clear()

    _run_exit(bot, book)

    assert len(fake.submitted) == 1
    assert book.positions == []
    assert book.closed[0]["exit_px"] == 123.45
    assert book.closed[0]["closed_at"] == FILL_TIME


def test_working_exit_is_not_submitted_again(executor, shadow_path):
    """A DAY sell that has not filled must not be stacked on the next cycle."""
    bot, fake = executor
    fake.positions.append(_broker_position("DTE", 5, avg="70"))
    book = Book(positions=[_position("DTE", 5)])

    _run_exit(bot, book)
    _run_exit(bot, book)

    assert len(fake.submitted) == 1
    assert book.closed == []
    assert book.positions[0].pending_exit_order_id == "submitted-1"
    assert "submitted-1" not in fake.cancelled


def test_run_cycle_reconciles_a_stop_before_the_exit_date(executor, shadow_path, monkeypatch):
    bot, fake = executor
    fake.orders.append(
        _order("stop-cl", "CL", status="filled", qty=23, price="84.20", filled_at=STOP_TIME)
    )
    book = Book(positions=[_position("CL", 23, close_on="2026-12-01")])
    monkeypatch.setattr("src.executor.load_book", lambda: book)
    monkeypatch.setattr("src.executor.save_book", lambda _book: None)
    monkeypatch.setattr("src.executor.enabled_bots", lambda: [])

    bot.run_cycle()

    assert fake.submitted == []
    assert book.positions == []
    assert book.closed[0]["exit_px"] == 84.20
    assert book.closed[0]["closed_at"] == STOP_TIME


def test_same_cycle_fill_uses_fill_price(executor, shadow_path):
    bot, fake = executor
    fake.fill_on_submit = ("55.25", FILL_TIME)
    fake.positions.append(_broker_position("FFIV", 2, avg="50"))
    book = Book(positions=[_position("FFIV", 2)])

    _run_exit(bot, book)

    assert len(fake.submitted) == 1
    assert book.positions == []
    assert book.closed[0]["exit_px"] == 55.25
    assert book.closed[0]["closed_at"] == FILL_TIME


def test_reconcile_books_filled_stop_and_flags_unexpected_short(executor, shadow_path, caplog):
    caplog.set_level(logging.INFO)
    bot, fake = executor
    # Stop filled before the planned exit date. Alpaca is flat in CL.
    fake.orders.append(
        _order("stop-cl", "CL", status="filled", qty=23, price="84.20", filled_at=STOP_TIME)
    )
    fake.positions.append(_broker_position("SCHW", 10, side="short", avg="96.77"))
    book = Book(positions=[_position("CL", 23, close_on="2026-12-01")])

    _run_exit(bot, book)

    assert fake.submitted == []
    assert [p.ticker for p in book.positions] == []
    assert book.closed[0]["ticker"] == "CL"
    assert book.closed[0]["exit_px"] == 84.20
    assert book.closed[0]["closed_at"] == STOP_TIME
    assert "UNEXPECTED SHORT SCHW qty=-10" in caplog.text
    assert "SCHW" not in {p.ticker for p in book.positions}


def test_reconcile_books_stop_not_the_later_short_open(executor, shadow_path, caplog):
    """A later sell that opened the short is not the long's exit price."""
    caplog.set_level(logging.INFO)
    bot, fake = executor
    fake.orders.extend(
        [
            _order("stop-cl", "CL", status="filled", qty=23, price="84.20", filled_at=STOP_TIME),
            _order("short-cl", "CL", status="filled", qty=23, price="84.80", filled_at=SHORT_TIME),
        ]
    )
    fake.positions.append(_broker_position("CL", 23, side="short", avg="84.80"))
    book = Book(positions=[_position("CL", 23)])

    bot.reconcile_book(book)

    assert fake.submitted == []
    assert book.positions == []
    assert book.closed[0]["exit_px"] == 84.20
    assert book.closed[0]["closed_at"] == STOP_TIME
    assert "UNEXPECTED SHORT CL qty=-23" in caplog.text


def test_timed_exit_cancels_resting_stop_before_selling(executor, shadow_path, caplog):
    caplog.set_level(logging.INFO)
    bot, fake = executor
    fake.positions.append(_broker_position("CL", 23, avg="80"))
    fake.orders.append(_order("gtc-stop", "CL", status="accepted", qty=23))
    book = Book(positions=[_position("CL", 23)])

    _run_exit(bot, book)

    assert "gtc-stop" in fake.cancelled
    assert len(fake.submitted) == 1
    assert int(fake.submitted[0].qty) == 23
    assert book.closed == []
    assert book.positions[0].pending_exit_order_id
    assert "resting stop cannot also fire" in caplog.text


def test_activity_price_and_time_used_when_order_omits_them(executor, shadow_path):
    bot, fake = executor
    fake.orders.append(
        _order("stop-cl", "CL", status="filled", qty=23, price=None, filled_at=None, filled_qty="23")
    )
    fake.activities.append(
        {
            "order_id": "stop-cl",
            "symbol": "CL",
            "side": "sell",
            "price": "84.20",
            "qty": "23",
            "transaction_time": STOP_TIME,
        }
    )
    book = Book(positions=[_position("CL", 23, close_on="2026-12-01")])

    bot.reconcile_book(book)

    assert fake.submitted == []
    assert book.positions == []
    assert book.closed[0]["exit_px"] == 84.20
    assert book.closed[0]["closed_at"] == STOP_TIME


def test_closing_fill_skips_shares_that_opened_the_short():
    stop = BrokerFill("stop-cl", 84.20, STOP_TIME, 23)
    opened_short = BrokerFill("short-cl", 84.80, SHORT_TIME, 23)
    chosen = select_closing_fill([opened_short, stop], broker_qty=-23)
    assert chosen is not None
    assert chosen.order_id == "stop-cl"
    assert chosen.price == 84.20
    assert select_closing_fill([stop], broker_qty=0) == stop


def test_dry_run_still_closes_book_without_broker(shadow_path):
    bot = Executor(_settings(), dry_run=True)
    book = Book(positions=[_position("DTE", 1)])

    bot._process_sells(book, TODAY)

    assert book.positions == []
    assert book.closed[0]["ticker"] == "DTE"
    assert book.closed[0]["exit_px"] is None


def test_signed_position_qty_marks_shorts_negative():
    assert signed_position_qty(SimpleNamespace(qty="10", side=_side("short"))) == -10
    assert signed_position_qty(SimpleNamespace(qty="-10", side=_side("short"))) == -10
    assert signed_position_qty(SimpleNamespace(qty="10", side=_side("long"))) == 10
    assert signed_position_qty(SimpleNamespace(qty="-3", side=None)) == -3


def test_old_book_position_still_loads():
    loaded = Position.from_dict(
        {
            "ticker": "CL",
            "qty": 23,
            "bot_id": "author",
            "bot_name": "Author",
            "signal_id": "CL:test:2026-09-01",
            "enter_on": "2026-09-01",
            "close_on": "2026-10-06",
            "opened_at": "2026-09-01T14:30:00+00:00",
            "future_field": "ignored",
        }
    )
    assert loaded.pending_exit_order_id is None
    assert loaded.entry_px is None
    assert loaded.qty == 23
