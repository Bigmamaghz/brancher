"""Exit safety for paper sells.

October 2026: GTC stops in CL and SCHW filled, but the book still showed those
longs. The next timed exit submitted DAY market sells and immediately wrote a
close into book.json, using the stop's fill price and the submission clock.
The account allows shorting, so those sells filled later as new shorts.

A sell here is only allowed to reduce a long that Alpaca still holds. The
book records a close only after that sell's fill (or after a reconcile finds
the fill that already closed the long).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from src.alpaca import (
    ActivityFill,
    BrokerFill,
    cancel_open_exit_orders,
    cancel_open_orders,
    cancel_sells_without_long,
    fetch_order,
    fill_from_order,
    get_position_entry_price,
    get_position_qty,
    get_signed_positions,
    index_sell_activities,
    latest_buy_fill_price,
    load_raw_activities,
    order_lifecycle,
    sell_fills,
    submit_sell,
)
from src.book import Book, Position
from src.shadow import record_closed

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcileEvent:
    kind: str  # closed | unexpected_short | unexpected_long
    symbol: str
    qty: int
    fill_price: float | None = None
    fill_time: str | None = None
    bot_name: str = "Brancher"
    close_on: str = ""


@dataclass(frozen=True)
class SellResult:
    kind: str  # wait | skip | submitted | filled | reconciled
    qty: int = 0
    order_id: str | None = None
    fill_price: float | None = None
    fill_time: str | None = None


def select_closing_fill(fills: list[BrokerFill], broker_qty: int) -> BrokerFill | None:
    """Pick the sell that closed the long.

    ``broker_qty`` is Alpaca's signed position now (0 flat, negative short).
    Sells that explain a current short are skipped, so a later short-opening
    sell is not booked as the long's exit. A single sell larger than the
    short (it closed the long and opened the short together) is kept.
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


def _unexpected(symbol: str, qty: int) -> ReconcileEvent:
    if qty < 0:
        logger.error(
            "UNEXPECTED SHORT %s qty=%s at Alpaca. Brancher will not buy or sell it.",
            symbol,
            qty,
        )
        kind = "unexpected_short"
    else:
        logger.error(
            "UNEXPECTED LONG %s qty=%s at Alpaca. Brancher will not buy or sell it.",
            symbol,
            qty,
        )
        kind = "unexpected_long"
    return ReconcileEvent(kind=kind, symbol=symbol, qty=qty)


def _note_open_long(client, pos: Position, held: int) -> None:
    if held != pos.qty:
        logger.warning(
            "Qty mismatch for %s: book has %s, Alpaca long qty is %s.",
            pos.ticker,
            pos.qty,
            held,
        )
    if pos.entry_px is None:
        try:
            pos.entry_px = get_position_entry_price(client, pos.ticker)
        except Exception as exc:
            logger.warning("Could not read entry price for %s: %s", pos.ticker, exc)


def _entry_price(client, pos: Position) -> float | None:
    if pos.entry_px is not None:
        return pos.entry_px
    try:
        return latest_buy_fill_price(client, pos.ticker)
    except Exception as exc:
        logger.warning("Could not read buy fill for %s: %s", pos.ticker, exc)
        return None


def _book_missing_long(
    client,
    book: Book,
    pos: Position,
    held: int,
    activities: dict[str, ActivityFill],
) -> ReconcileEvent | None:
    """Book ``pos`` from the real closing fill when Alpaca is no longer long."""
    if held > 0:
        return None
    try:
        fills = sell_fills(client, pos.ticker, activities)
    except Exception as exc:
        logger.error(
            "Reconcile %s: could not read fills (%s). Not selling and not booking a guessed close.",
            pos.ticker,
            exc,
        )
        return None
    fill = select_closing_fill(fills, held)
    if fill is None:
        logger.error(
            "Reconcile %s: book has an open long but Alpaca qty is %s, and no closing sell fill "
            "was found. Not selling.",
            pos.ticker,
            held,
        )
        return None
    record_closed(
        book,
        pos.ticker,
        pos.bot_id,
        pos.signal_id,
        pos.enter_on,
        pos.close_on,
        _entry_price(client, pos),
        fill.price,
        fill.filled_at,
    )
    book.close_position(pos.ticker)
    logger.warning(
        "Reconcile %s: book had an open long but Alpaca qty is %s. "
        "Booking close at actual fill %s time %s order %s.",
        pos.ticker,
        held,
        fill.price,
        fill.filled_at,
        fill.order_id,
    )
    return ReconcileEvent(
        kind="closed",
        symbol=pos.ticker,
        qty=pos.qty,
        fill_price=fill.price,
        fill_time=fill.filled_at,
        bot_name=pos.bot_name,
        close_on=pos.close_on,
    )


def reconcile_book(client, book: Book) -> list[ReconcileEvent]:
    """Make the book match Alpaca before any new exit is considered.

    Longs the book still shows that Alpaca has already closed are booked at
    the actual fill. Positions Alpaca holds that the book does not know about
    are reported and left alone.
    """
    events: list[ReconcileEvent] = []
    try:
        broker = get_signed_positions(client)
    except Exception as exc:
        logger.error("Reconcile skipped: could not read Alpaca positions (%s).", exc)
        return events
    try:
        cancel_sells_without_long(client, broker)
    except Exception as exc:
        logger.error("Reconcile could not cancel resting sells (%s).", exc)
    try:
        broker = get_signed_positions(client)
    except Exception as exc:
        logger.error("Reconcile skipped after cancel: could not re-read positions (%s).", exc)
        return events

    activities = index_sell_activities(load_raw_activities(client))
    book_symbols = {pos.ticker for pos in book.positions}
    for symbol, qty in sorted(broker.items()):
        if symbol in book_symbols or qty == 0:
            continue
        events.append(_unexpected(symbol, qty))

    for pos in list(book.positions):
        held = broker.get(pos.ticker, 0)
        if held > 0:
            _note_open_long(client, pos, held)
            continue
        closed = _book_missing_long(client, book, pos, held, activities)
        if closed is not None:
            events.append(closed)
        if held < 0:
            events.append(_unexpected(pos.ticker, held))
    return events


def _book_our_fill(client, book: Book, pos: Position, order, activities: dict[str, ActivityFill]) -> SellResult:
    try:
        held = get_position_qty(client, pos.ticker)
    except Exception as exc:
        logger.error(
            "Exit order %s for %s looks filled, but the Alpaca position could not be read (%s). "
            "Book left open.",
            getattr(order, "id", "?"),
            pos.ticker,
            exc,
        )
        return SellResult("skip")
    if held < 0:
        logger.error(
            "Exit order for %s filled but Alpaca is short %s. "
            "Not treating that sell as the long's close.",
            pos.ticker,
            held,
        )
        closed = _book_missing_long(client, book, pos, held, activities)
        if closed is None:
            return SellResult("skip")
        return SellResult(
            "reconciled",
            qty=pos.qty,
            fill_price=closed.fill_price,
            fill_time=closed.fill_time,
        )
    fill = fill_from_order(order, activities.get(str(order.id)))
    if fill is None:
        logger.error(
            "Exit order %s for %s is filled but has no fill price/time. Book left open.",
            getattr(order, "id", "?"),
            pos.ticker,
        )
        return SellResult("skip")
    record_closed(
        book,
        pos.ticker,
        pos.bot_id,
        pos.signal_id,
        pos.enter_on,
        pos.close_on,
        _entry_price(client, pos),
        fill.price,
        fill.filled_at,
    )
    book.close_position(pos.ticker)
    logger.info(
        "Booked close for %s at fill %s time %s order %s.",
        pos.ticker,
        fill.price,
        fill.filled_at,
        fill.order_id,
    )
    if held > 0:
        logger.error(
            "UNEXPECTED LONG %s qty=%s remains at Alpaca after the exit fill. "
            "Brancher closed the book position and will not trade the leftover.",
            pos.ticker,
            held,
        )
    return SellResult(
        "filled",
        qty=fill.qty,
        order_id=fill.order_id,
        fill_price=fill.price,
        fill_time=fill.filled_at,
    )


def _activities(client) -> dict[str, ActivityFill]:
    return index_sell_activities(load_raw_activities(client))


def sell_due_position(client, book: Book, pos: Position) -> SellResult:
    """Submit an exit sell that cannot open or increase a short.

    An exit already working is left alone. Other open sells (GTC stops) are
    cancelled first so two exits cannot both fire. The book is not closed
    until Alpaca reports a fill.
    """
    if pos.pending_exit_order_id:
        pending = fetch_order(client, pos.pending_exit_order_id)
        if pending is None:
            logger.warning(
                "Pending exit %s for %s was not found. Evaluating a new exit.",
                pos.pending_exit_order_id,
                pos.ticker,
            )
            pos.pending_exit_order_id = None
        else:
            life = order_lifecycle(pending)
            if life == "open":
                try:
                    cancelled, _still = cancel_open_exit_orders(
                        client,
                        pos.ticker,
                        keep_ids={pos.pending_exit_order_id},
                    )
                except Exception as exc:
                    logger.error(
                        "Could not review other exit orders for %s (%s). Leaving the working exit %s.",
                        pos.ticker,
                        exc,
                        pos.pending_exit_order_id,
                    )
                    cancelled = 0
                if cancelled:
                    logger.info(
                        "Cancelled %d other exit order(s) for %s while waiting for fill of %s.",
                        cancelled,
                        pos.ticker,
                        pos.pending_exit_order_id,
                    )
                logger.info(
                    "Exit for %s already working as order %s. Not submitting another sell.",
                    pos.ticker,
                    pos.pending_exit_order_id,
                )
                return SellResult("wait", order_id=pos.pending_exit_order_id)
            if life == "filled":
                return _book_our_fill(client, book, pos, pending, _activities(client))
            logger.warning(
                "Pending exit %s for %s is %s, not a fill. Evaluating a new exit.",
                pos.pending_exit_order_id,
                pos.ticker,
                life,
            )
            pos.pending_exit_order_id = None

    try:
        cancelled, still_open = cancel_open_exit_orders(client, pos.ticker)
    except Exception as exc:
        logger.error(
            "Skipping exit sell for %s: could not account for open exit orders (%s).",
            pos.ticker,
            exc,
        )
        return SellResult("skip")
    if cancelled:
        logger.info(
            "Cancelled %d open exit order(s) for %s before the timed exit "
            "so a resting stop cannot also fire.",
            cancelled,
            pos.ticker,
        )
    if still_open:
        logger.warning(
            "Skipping exit sell for %s: %d exit order(s) still open after cancel. "
            "Not submitting a second exit.",
            pos.ticker,
            still_open,
        )
        return SellResult("skip")

    try:
        held = get_position_qty(client, pos.ticker)
    except Exception as exc:
        logger.error(
            "Skipping exit sell for %s: could not read the Alpaca position (%s).",
            pos.ticker,
            exc,
        )
        return SellResult("skip")
    if held <= 0:
        logger.warning(
            "Skipping exit sell for %s: no long position at Alpaca (qty=%s). "
            "A sell would open or increase a short.",
            pos.ticker,
            held,
        )
        if held < 0:
            logger.error(
                "UNEXPECTED SHORT %s qty=%s while exiting. Not selling.",
                pos.ticker,
                held,
            )
        closed = _book_missing_long(client, book, pos, held, _activities(client))
        if closed is None:
            return SellResult("skip")
        return SellResult(
            "reconciled",
            qty=pos.qty,
            fill_price=closed.fill_price,
            fill_time=closed.fill_time,
        )

    sell_qty = min(int(pos.qty), held)
    if sell_qty <= 0:
        logger.warning(
            "Skipping exit sell for %s: computed qty is %s.",
            pos.ticker,
            sell_qty,
        )
        return SellResult("skip")
    if sell_qty < pos.qty:
        logger.warning(
            "Capping exit sell for %s from %s to %s shares (Alpaca long qty is smaller).",
            pos.ticker,
            pos.qty,
            sell_qty,
        )

    if pos.entry_px is None:
        try:
            pos.entry_px = get_position_entry_price(client, pos.ticker)
        except Exception as exc:
            logger.warning("Could not read entry price for %s: %s", pos.ticker, exc)

    order_id = _submit_exit(client, pos.ticker, sell_qty)
    if order_id is None:
        return SellResult("skip")

    pos.pending_exit_order_id = order_id
    order = fetch_order(client, order_id)
    if order is not None and order_lifecycle(order) == "filled":
        return _book_our_fill(client, book, pos, order, _activities(client))

    logger.info(
        "Submitted exit sell for %s qty=%s order=%s. "
        "Book stays open until Alpaca confirms the fill.",
        pos.ticker,
        sell_qty,
        order_id,
    )
    return SellResult("submitted", qty=sell_qty, order_id=order_id)


def _submit_exit(client, symbol: str, qty: int) -> str | None:
    try:
        return submit_sell(client, symbol, qty)
    except Exception as exc:
        text = str(exc)
        if "wash trade" not in text.lower() and "40310000" not in text:
            logger.error("Sell failed for %s: %s", symbol, exc)
            return None
        cancelled = cancel_open_orders(client, symbol)
        if not cancelled:
            logger.error("Sell failed for %s: %s", symbol, exc)
            return None
        try:
            order_id = submit_sell(client, symbol, qty)
        except Exception as exc2:
            logger.error("Sell retry failed for %s: %s", symbol, exc2)
            return None
        logger.info(
            "Sell for %s succeeded after cancelling %d open order(s)",
            symbol,
            cancelled,
        )
        return order_id
