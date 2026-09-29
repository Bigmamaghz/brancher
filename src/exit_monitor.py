from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.alpaca import get_position_entry_price, submit_sell
from src.book import Book

logger = logging.getLogger(__name__)

WATCHERS_PATH = Path("/Users/mybot/joe/fleet/position_watchers.json")

EXIT_STATES = ("AT_SL", "AT_TP")
REASON = {"AT_SL": "stop-loss hit", "AT_TP": "take-profit hit"}


def load_states(path: Path | None = None) -> dict[str, dict]:
    """Read live watcher states produced by fleet/position_watchers.py."""
    p = path or WATCHERS_PATH
    try:
        data = json.loads(p.read_text())
    except Exception as exc:
        logger.warning("exit_monitor: cannot read %s: %s", p, exc)
        return {}
    rows = data.get("rows")
    return rows if isinstance(rows, dict) else {}


def run_exit_monitor(self, book: Book) -> None:
    """Fast exits: sell intraday when a watcher flags AT_SL / AT_TP.

    Levels come from joe_levels.levels(): SL = entry - max(ATR14, 0.5%),
    TP = entry + 2R. Sell path is identical to _process_sells: submit_sell,
    telegram notice, record_closed, book.close_position. Positions sold
    here drop out of _process_sells naturally (already removed).
    """
    if not self.client:
        return
    states = load_states()
    if not states:
        return
    open_tickers = {p.ticker for p in book.positions}
    for ticker, row in states.items():
        if ticker not in open_tickers:
            continue
        state = row.get("state")
        if state not in EXIT_STATES:
            continue
        pos = next(p for p in book.positions if p.ticker == ticker)
        w_qty = int(row.get("qty", pos.qty))
        if pos.qty != w_qty:
            logger.warning(
                "exit_monitor: %s qty mismatch book=%s watcher=%s — skipping",
                ticker, pos.qty, w_qty,
            )
            continue
        entry_px = None
        try:
            entry_px = get_position_entry_price(self.client, ticker)
        except Exception:
            pass
        try:
            order_id = submit_sell(self.client, ticker, pos.qty)
        except Exception as exc:
            # Wash-trade guard: unfilled opposite-side order blocks the sell.
            if "wash trade" in str(exc) or "40310000" in str(exc):
                try:
                    from src.alpaca import cancel_open_orders

                    cancelled = cancel_open_orders(self.client, ticker)
                    if cancelled:
                        order_id = submit_sell(self.client, ticker, pos.qty)
                        logger.info("exit_monitor: %s sell ok after cancelling %d order(s)", ticker, cancelled)
                    else:
                        logger.error("exit_monitor: sell blocked for %s: %s", ticker, exc)
                        continue
                except Exception as exc2:
                    logger.error("exit_monitor: sell retry failed for %s: %s", ticker, exc2)
                    continue
            else:
                logger.error("exit_monitor: sell failed for %s: %s", ticker, exc)
                continue
        level = row.get("sl") if state == "AT_SL" else row.get("tp")
        from src.telegram import format_message

        msg = format_message(
            kind="SELL",
            source=pos.bot_name,
            signal_line=ticker,
            action=f"SELL sell qty={pos.qty} ({REASON[state]} {level})",
            detail=f"order={order_id}",
            dates=f"FAST EXIT: live {state} (was due {pos.close_on})",
        )
        self.telegram.send(msg, dry_run=self.dry_run)
        exit_px = None
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest

            for o in self.client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.CLOSED, symbols=[ticker], limit=5)
            ):
                if o.side.value == "sell" and o.filled_avg_price:
                    exit_px = float(o.filled_avg_price)
                    break
        except Exception:
            exit_px = None
        from src.shadow import record_closed

        rec = record_closed(
            book, ticker, pos.bot_id, pos.signal_id, pos.enter_on,
            pos.close_on, entry_px, exit_px,
            datetime.now(timezone.utc).isoformat(),
        )
        rec["exit_reason"] = state
        book.close_position(ticker)
        logger.info("exit_monitor: %s %s sold (order %s)", ticker, state, order_id)
