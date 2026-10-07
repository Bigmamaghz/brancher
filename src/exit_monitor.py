from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.alpaca import cancel_order_by_id, get_position_entry_price, open_stop_orders, submit_sell
from src.book import Book

logger = logging.getLogger(__name__)

WATCHERS_PATH = Path("/Users/mybot/joe/fleet/position_watchers.json")

EXIT_STATES = ("AT_SL", "AT_TP")
REASON = {"AT_SL": "stop-loss hit", "AT_TP": "take-profit hit"}

def get_bar_range(client, ticker: str):
    """Latest completed 1-minute bar (low, high) for ticker.

    Consolidated bar, SIP primary with IEX fallback, RAW adjustment. Returns
    (None, None) when no bar is reachable so the caller can fall back.
    """
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import Adjustment

        key = getattr(client, "_api_key", None) or getattr(client, "api_key", None)
        sec = getattr(client, "_secret_key", None) or getattr(client, "secret_key", None)
        if not key or not sec:
            return None, None
        dc = StockHistoricalDataClient(key, sec)
        for feed in ("sip", "iex"):
            try:
                bars = dc.get_stock_bars(
                    StockBarsRequest(
                        symbol_or_symbols=ticker,
                        timeframe=TimeFrame.Minute,
                        limit=2,
                        feed=feed,
                        adjustment=Adjustment.RAW,
                    )
                )
                rows = bars.data.get(ticker, [])
                if rows:
                    return float(rows[-1].low), float(rows[-1].high)
            except Exception:
                continue
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("exit_monitor: bar range unavailable for %s: %s", ticker, exc)
    return None, None


def breach_confirmed(low, high, state: str, row: dict) -> bool:
    """Confirm an AT_SL/AT_TP breach from the 1-minute bar, not a single tick.

    AT_SL needs bar low <= sl; AT_TP needs bar high >= tp. When no bar is
    reachable, fall back to the watcher's live price for that row.
    """
    level = row.get("sl") if state == "AT_SL" else row.get("tp")
    if level is None:
        return True
    try:
        level = float(level)
    except Exception:
        return True
    if state == "AT_SL":
        if low is not None:
            return low <= level
        try:
            return float(row.get("current")) <= level
        except Exception:
            return True
    if high is not None:
        return high >= level
    try:
        return float(row.get("current")) >= level
    except Exception:
        return True



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
        # Confirm the breach from the consolidated 1-minute bar (not one IEX tick),
        # and recheck immediately before sending the exit.
        low, high = get_bar_range(self.client, ticker)
        if not breach_confirmed(low, high, state, row):
            logger.warning(
                "exit_monitor: %s %s NOT confirmed by 1-min bar "
                "(low=%s high=%s level=%s) — aborting exit",
                ticker, state, low, high,
                row.get("sl") if state == "AT_SL" else row.get("tp"),
            )
            continue
        entry_px = None
        try:
            entry_px = get_position_entry_price(self.client, ticker)
        except Exception:
            pass
        # Clear this ticker's resting protective stop so the manual sell cannot
        # double-fill. If a stop exists and cannot be cancelled, abort the sell.
        blocked = False
        for so in open_stop_orders(self.client, ticker):
            if cancel_order_by_id(self.client, so.id):
                logger.info("exit_monitor: %s cancelled resting stop %s before manual exit", ticker, so.id)
            else:
                logger.error("exit_monitor: %s could not cancel resting stop %s — aborting sell", ticker, so.id)
                blocked = True
                break
        if blocked:
            continue
        # Live-quantity guard: never sell more than Alpaca actually holds.
        # qty_available excludes shares held by a resting stop; a read failure
        # (None) fails closed. Refuse if flat/short (<=0).
        from src.alpaca import get_position_qty

        avail = get_position_qty(self.client, ticker)
        if avail is None or avail <= 0:
            logger.warning(
                "live-qty guard: %s skip exit (book qty=%s, live available=%s) — not selling",
                ticker, pos.qty, avail,
            )
            continue
        sell_qty = min(pos.qty, avail)
        try:
            order_id = submit_sell(self.client, ticker, sell_qty)
        except Exception as exc:
            # Wash-trade guard: unfilled opposite-side order blocks the sell.
            if "wash trade" in str(exc) or "40310000" in str(exc):
                try:
                    from src.alpaca import cancel_open_orders

                    cancelled = cancel_open_orders(self.client, ticker)
                    if cancelled:
                        order_id = submit_sell(self.client, ticker, sell_qty)
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
            o = self.client.get_order_by_id(order_id)
            o_status = str(getattr(o.status, "value", o.status)).lower()
            if o_status == "filled" and o.filled_avg_price:
                exit_px = float(o.filled_avg_price)
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
