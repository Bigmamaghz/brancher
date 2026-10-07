from __future__ import annotations

from src.config import PaperOnlyError, Settings, _validate_paper_url


def get_trading_client(settings: Settings):
    """Return Alpaca TradingClient configured for paper only."""
    from alpaca.trading.client import TradingClient

    if settings.alpaca_base_url:
        _validate_paper_url(settings.alpaca_base_url)

    return TradingClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        paper=True,
    )


def submit_buy(client, symbol: str, qty: int) -> str:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    order = client.submit_order(
        MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )
    )
    return str(order.id)


def submit_sell(client, symbol: str, qty: int) -> str:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    order = client.submit_order(
        MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
    )
    return str(order.id)


def cancel_open_orders(client, symbol: str) -> int:
    """Cancel open (unfilled) MARKET orders for symbol. Returns count cancelled.

    Only market orders are cancelled — they are the conflicting side that triggers
    a wash-trade (40310000) reject. Protective STOP, STOP_LIMIT, LIMIT and
    TRAILING_STOP orders are NEVER touched.
    """
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    n = 0
    try:
        for o in client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])):
            otype = getattr(o.order_type, "value", o.order_type)
            otype = str(otype).lower()
            if otype != "market":
                continue  # never cancel protective (stop/stop_limit/limit/trailing_stop)
            client.cancel_order(o.id)
            n += 1
    except Exception:
        pass
    return n


def get_equity(client) -> float:
    account = client.get_account()
    return float(account.equity)


def get_open_positions(client) -> dict[str, int]:
    positions = client.get_all_positions()
    return {p.symbol: int(float(p.qty)) for p in positions}

def get_position_entry_price(client, symbol: str) -> float | None:
    """Return avg entry price for an open paper position, else None."""
    for p in client.get_all_positions():
        if p.symbol == symbol:
            return float(p.avg_entry_price)
    return None


def get_position_qty(client, symbol: str) -> int | None:
    """Live qty_available for symbol, or None if it cannot be read.

    Uses qty_available (not qty) because shares already held by a resting
    protective STOP are excluded from it. Returns None on ANY error so the
    caller can fail closed and refuse to sell when the live state is unknown.
    A flat/short symbol returns 0 or a negative number.
    """
    try:
        for p in client.get_all_positions():
            if p.symbol == symbol:
                av = getattr(p, "qty_available", None)
                if av is None:
                    return None  # fail closed: unknown availability, never fall back to qty
                return int(float(av))
        return 0
    except Exception:
        return None


def market_is_open(client) -> bool | None:
    """Alpaca clock is_open, or None if it cannot be read (fail closed)."""
    try:
        return bool(client.get_clock().is_open)
    except Exception:
        return None


def open_stop_orders(client, symbol: str) -> list:
    """Open protective orders (STOP / STOP_LIMIT / TRAILING_STOP) for symbol.

    Read-only discovery so exit_monitor can clear a resting stop before a manual
    sell. Market/limit orders are intentionally excluded.
    """
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus

    out = []
    try:
        for o in client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])):
            otype = str(getattr(o.order_type, "value", o.order_type)).lower()
            if otype in ("stop", "stop_limit", "trailing_stop"):
                out.append(o)
    except Exception:
        pass
    return out


def cancel_order_by_id(client, order_id) -> bool:
    """Cancel exactly one order by id. Returns True on success.

    Used ONLY by exit_monitor's manual-exit path to clear that ticker's resting
    stop before selling. cancel_open_orders() stays market-only and untouched.
    """
    try:
        client.cancel_order(order_id)
        return True
    except Exception:
        return False
