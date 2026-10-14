"""Match the book to Alpaca, and record a close only from a confirmed fill.

This module does not submit, cancel, or replace orders. A resting protective
stop is left alone. A position Alpaca holds that the book does not know about
is reported once and is not added to the book and not traded.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

import src.shadow as shadow
from src.book import Book, Position

logger = logging.getLogger(__name__)

_OPEN_STATUSES = {
    "new",
    "accepted",
    "pending_new",
    "partially_filled",
    "accepted_for_bidding",
    "pending_cancel",
    "pending_replace",
    "pending_review",
    "stopped",
    "calculated",
    "held",
}
_DEAD_STATUSES = {
    "canceled",
    "cancelled",
    "expired",
    "rejected",
    "suspended",
    "replaced",
}


@dataclass(frozen=True)
class BrokerFill:
    order_id: str
    price: float
    filled_at: str
    qty: int


@dataclass(frozen=True)
class PendingResult:
    state: str  # booked | working | clear
    price: float | None = None
    filled_at: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ReconcileEvent:
    kind: str  # closed | unexpected_short | unexpected_long
    symbol: str
    qty: int
    fill_price: float | None = None
    fill_time: str | None = None
    bot_name: str = "Brancher"
    close_on: str = ""


def _enum_str(value) -> str:
    if value is None:
        return ""
    raw = getattr(value, "value", value)
    return str(raw).lower()


def _as_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_qty(value) -> int:
    number = _as_float(value)
    if number is None:
        return 0
    return int(number)


def _as_iso(value) -> str | None:
    if value is None or value == "":
        return None
    if hasattr(value, "isoformat"):
        text = value.isoformat()
    else:
        text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        return text[:-1] + "+00:00"
    return text


def _row(row, name: str):
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _signed_qty(position) -> int | None:
    """Share count from ``qty`` (not qty_available). Shorts are negative.

    qty_available is 0 while a protective stop reserves the shares, and the
    long is still open. Reconcile must not treat that as a closed position.
    """
    try:
        raw = int(float(position.qty))
    except (TypeError, ValueError, AttributeError):
        return None
    side = _enum_str(getattr(position, "side", None))
    if side == "short":
        return -abs(raw)
    if side == "long":
        return abs(raw)
    return raw


def _order_fill(order, activity: tuple[float, str, int] | None = None) -> BrokerFill | None:
    if _enum_str(getattr(order, "status", None)) != "filled":
        return None
    side = _enum_str(getattr(order, "side", None))
    if side and side != "sell":
        return None
    price = _as_float(getattr(order, "filled_avg_price", None))
    filled_at = _as_iso(getattr(order, "filled_at", None))
    qty = _as_qty(getattr(order, "filled_qty", None) or getattr(order, "qty", None))
    if activity is not None:
        price = activity[0]
        if activity[1]:
            filled_at = activity[1]
        if qty <= 0 and activity[2] > 0:
            qty = activity[2]
    if price is None or not filled_at or qty <= 0:
        return None
    return BrokerFill(
        order_id=str(getattr(order, "id", "")),
        price=float(price),
        filled_at=filled_at,
        qty=qty,
    )


def _activity_index(client) -> dict[str, tuple[float, str, int]]:
    """order_id -> (vwap, latest transaction time, qty) for sell FILL activities."""
    rows: list = []
    reader = getattr(client, "get_fill_activities", None)
    if callable(reader):
        try:
            rows = list(reader())
        except Exception as exc:
            logger.warning("Fill activities reader failed: %s", exc)
            rows = []
    elif isinstance(client, TradingClient):
        try:
            payload = client.get(
                "/account/activities",
                {"activity_types": "FILL", "page_size": 100, "direction": "desc"},
            )
        except Exception as exc:
            logger.warning("Alpaca fill activities unavailable: %s", exc)
            payload = None
        if isinstance(payload, list):
            rows = payload
    grouped: dict[str, list[tuple[float, float, str]]] = {}
    for row in rows:
        if _enum_str(_row(row, "side")) != "sell":
            continue
        order_id = str(_row(row, "order_id") or "")
        price = _as_float(_row(row, "price"))
        qty = _as_float(_row(row, "qty"))
        when = _as_iso(_row(row, "transaction_time")) or ""
        if not order_id or price is None or qty is None or qty <= 0:
            continue
        grouped.setdefault(order_id, []).append((price, qty, when))
    indexed: dict[str, tuple[float, str, int]] = {}
    for order_id, parts in grouped.items():
        total = sum(qty for _, qty, _ in parts)
        vwap = sum(price * qty for price, qty, _ in parts) / total
        when = max(parts, key=lambda part: part[2])[2]
        indexed[order_id] = (vwap, when, _as_qty(total))
    return indexed


def read_sell_fill(client, order_id: str) -> tuple[float, str] | None:
    """Price and fill time for this order id, or None if it is not a confirmed fill.

    Does not look at other orders, and does not substitute the current clock.
    """
    getter = getattr(client, "get_order_by_id", None)
    if not callable(getter):
        return None
    try:
        order = getter(order_id)
    except Exception:
        return None
    if _enum_str(getattr(order, "status", None)) != "filled":
        return None
    side = _enum_str(getattr(order, "side", None))
    if side and side != "sell":
        return None
    activity = _activity_index(client).get(str(order_id))
    fill = _order_fill(order, activity)
    if fill is None:
        return None
    return fill.price, fill.filled_at


def _pending_status(client, order_id: str) -> str:
    """filled, open, dead, or unknown. Unknown fails closed (do not sell again)."""
    getter = getattr(client, "get_order_by_id", None)
    if not callable(getter):
        return "unknown"
    try:
        order = getter(order_id)
    except Exception:
        return "unknown"
    status = _enum_str(getattr(order, "status", None))
    if status == "filled":
        return "filled"
    if status in _DEAD_STATUSES:
        return "dead"
    if status in _OPEN_STATUSES:
        return "open"
    return "unknown"


def book_confirmed_close(
    book: Book,
    pos: Position,
    entry_px: float | None,
    price: float,
    filled_at: str,
    reason: str | None = None,
) -> dict:
    rec = shadow.record_closed(
        book,
        pos.ticker,
        pos.bot_id,
        pos.signal_id,
        pos.enter_on,
        pos.close_on,
        entry_px,
        price,
        filled_at,
    )
    if reason:
        rec["exit_reason"] = reason
    book.close_position(pos.ticker)
    return rec


def settle_pending_exit(client, book: Book, pos: Position) -> PendingResult:
    """Book a pending exit if it filled. Never submits or cancels."""
    order_id = pos.pending_exit_order_id
    if not order_id:
        return PendingResult("clear")
    reason = pos.pending_exit_reason
    fill = read_sell_fill(client, order_id)
    if fill is not None:
        price, filled_at = fill
        book_confirmed_close(book, pos, pos.entry_px, price, filled_at, reason)
        logger.info(
            "Booked close for %s at fill %s time %s order %s.",
            pos.ticker,
            price,
            filled_at,
            order_id,
        )
        return PendingResult("booked", price=price, filled_at=filled_at, reason=reason)
    status = _pending_status(client, order_id)
    if status == "dead":
        logger.warning(
            "Pending exit %s for %s is no longer working. A new exit may be evaluated.",
            order_id,
            pos.ticker,
        )
        pos.pending_exit_order_id = None
        pos.pending_exit_reason = None
        return PendingResult("clear")
    if status == "filled":
        logger.error(
            "Exit order %s for %s is filled but has no fill price/time. "
            "Book left open. Not submitting another sell.",
            order_id,
            pos.ticker,
        )
    else:
        logger.info(
            "Exit for %s already working as order %s. Not submitting another sell.",
            pos.ticker,
            order_id,
        )
    return PendingResult("working", reason=reason)


def select_closing_fill(fills: list[BrokerFill], broker_qty: int) -> BrokerFill | None:
    """The sell that closed the long.

    Sells that explain a current short are skipped, so a later short-opening
    sell is not booked as the long's exit.
    """
    if broker_qty > 0:
        return None
    short_left = -broker_qty if broker_qty < 0 else 0
    for fill in sorted(fills, key=lambda item: item.filled_at, reverse=True):
        if fill.qty <= 0:
            continue
        if short_left <= 0:
            return fill
        if fill.qty <= short_left:
            short_left -= fill.qty
            continue
        return fill
    return None


def _closed_sell_fills(client, symbol: str, activities: dict[str, tuple[float, str, int]]) -> list[BrokerFill]:
    try:
        orders = client.get_orders(
            filter=GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                symbols=[symbol],
                side=OrderSide.SELL,
                limit=100,
            )
        )
    except Exception as exc:
        logger.error("Reconcile %s: could not read closed orders (%s). Not booking a guessed close.", symbol, exc)
        return []
    fills: list[BrokerFill] = []
    for order in orders:
        symbol_on_order = getattr(order, "symbol", None)
        if symbol_on_order and symbol_on_order != symbol:
            continue
        fill = _order_fill(order, activities.get(str(getattr(order, "id", ""))))
        if fill is not None:
            fills.append(fill)
    return fills


def _unexpected(symbol: str, qty: int) -> ReconcileEvent:
    if qty < 0:
        logger.error(
            "UNEXPECTED SHORT %s qty=%s at Alpaca. Not adding it to the book and not trading it.",
            symbol,
            qty,
        )
        kind = "unexpected_short"
    else:
        logger.error(
            "UNEXPECTED LONG %s qty=%s at Alpaca. Not adding it to the book and not trading it.",
            symbol,
            qty,
        )
        kind = "unexpected_long"
    return ReconcileEvent(kind=kind, symbol=symbol, qty=qty)


def _broker_qty(client) -> dict[str, int | None] | None:
    try:
        positions = list(client.get_all_positions())
    except Exception as exc:
        logger.error("Reconcile skipped: could not read Alpaca positions (%s).", exc)
        return None
    qty_by_symbol: dict[str, int | None] = {}
    for position in positions:
        symbol = getattr(position, "symbol", None)
        if not symbol:
            continue
        qty_by_symbol[str(symbol)] = _signed_qty(position)
    return qty_by_symbol


def reconcile_book(client, book: Book) -> list[ReconcileEvent]:
    """Book longs Alpaca has already closed, using the actual fill.

    Held longs stay in the book even when qty_available is 0 (a resting stop
    reserves those shares). Nothing is cancelled or submitted. Orphan broker
    positions are reported and left out of the book.
    """
    qty_by_symbol = _broker_qty(client)
    if qty_by_symbol is None:
        return []
    events: list[ReconcileEvent] = []
    activities = _activity_index(client)
    book_symbols = {pos.ticker for pos in book.positions}
    for symbol, qty in sorted(qty_by_symbol.items()):
        if qty is None or qty == 0 or symbol in book_symbols:
            continue
        events.append(_unexpected(symbol, qty))

    for pos in list(book.positions):
        if pos.ticker not in qty_by_symbol:
            held: int | None = 0
        else:
            held = qty_by_symbol[pos.ticker]
        if held is None:
            logger.error(
                "Reconcile %s: qty unreadable. Leaving the book row and not selling.",
                pos.ticker,
            )
            continue
        if held > 0:
            continue
        fill = select_closing_fill(_closed_sell_fills(client, pos.ticker, activities), held)
        if fill is None:
            logger.error(
                "Reconcile %s: book has an open long but Alpaca qty is %s, and no closing "
                "sell fill was found. Not booking a guessed close and not selling.",
                pos.ticker,
                held,
            )
        else:
            book_confirmed_close(book, pos, pos.entry_px, fill.price, fill.filled_at, pos.pending_exit_reason)
            logger.warning(
                "Reconcile %s: book had an open long but Alpaca qty is %s. "
                "Booking close at actual fill %s time %s order %s.",
                pos.ticker,
                held,
                fill.price,
                fill.filled_at,
                fill.order_id,
            )
            events.append(
                ReconcileEvent(
                    kind="closed",
                    symbol=pos.ticker,
                    qty=pos.qty,
                    fill_price=fill.price,
                    fill_time=fill.filled_at,
                    bot_name=pos.bot_name,
                    close_on=pos.close_on,
                )
            )
        if held < 0:
            events.append(_unexpected(pos.ticker, held))
    return events
