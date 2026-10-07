from __future__ import annotations

import logging
from dataclasses import dataclass

from alpaca.common.enums import Sort
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

from src.config import Settings, _validate_paper_url

try:
    from alpaca.trading.enums import PositionIntent
except ImportError:  # alpaca-py < 0.35
    PositionIntent = None

logger = logging.getLogger(__name__)

# Statuses that mean the order can still trade. Anything else is done.
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


@dataclass(frozen=True)
class ActivityFill:
    """One order's sell executions taken from Alpaca FILL activities."""

    order_id: str
    price: float
    filled_at: str
    qty: int


@dataclass(frozen=True)
class BrokerFill:
    """A filled sell, priced and timed from the order and/or its FILL activity."""

    order_id: str
    price: float
    filled_at: str
    qty: int


def get_trading_client(settings: Settings):
    """Return Alpaca TradingClient configured for paper only."""
    if settings.alpaca_base_url:
        _validate_paper_url(settings.alpaca_base_url)

    return TradingClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        paper=True,
    )


def submit_buy(client, symbol: str, qty: int) -> str:
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
    """Submit a DAY market sell that is only allowed to close a long.

    ``position_intent=sell_to_close`` (alpaca-py >= 0.35) tells Alpaca to
    reject the order if it would open or increase a short. Callers still have
    to check the live long qty themselves so they can skip or cap first.
    """
    payload = {
        "symbol": symbol,
        "qty": qty,
        "side": OrderSide.SELL,
        "time_in_force": TimeInForce.DAY,
    }
    if PositionIntent is not None:
        payload["position_intent"] = PositionIntent.SELL_TO_CLOSE
    order = client.submit_order(MarketOrderRequest(**payload))
    return str(order.id)


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
    if value is None:
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


def signed_position_qty(position) -> int:
    """Positive for a long, negative for a short.

    Alpaca sometimes sends an absolute ``qty`` plus ``side``, and sometimes a
    signed ``qty``. Both shapes are handled.
    """
    raw = int(float(position.qty))
    side = _enum_str(getattr(position, "side", None))
    if side == "short":
        return -abs(raw)
    if side == "long":
        return abs(raw)
    return raw


def get_signed_positions(client) -> dict[str, int]:
    positions = {}
    for position in client.get_all_positions():
        qty = signed_position_qty(position)
        if qty != 0:
            positions[position.symbol] = qty
    return positions


def get_open_positions(client) -> dict[str, int]:
    """Signed share counts. Shorts are negative."""
    return get_signed_positions(client)


def get_position_qty(client, symbol: str) -> int:
    """Signed qty for ``symbol``. ``0`` when Alpaca is flat."""
    return get_signed_positions(client).get(symbol, 0)


def get_position_entry_price(client, symbol: str) -> float | None:
    """Return avg entry price for an open paper position, else None."""
    for position in client.get_all_positions():
        if position.symbol == symbol:
            raw = getattr(position, "avg_entry_price", None)
            if raw is None or raw == "":
                return None
            return float(raw)
    return None


def get_equity(client) -> float:
    account = client.get_account()
    return float(account.equity)


def is_sell_order(order) -> bool:
    return _enum_str(getattr(order, "side", None)) == "sell"


def order_lifecycle(order) -> str:
    """``filled``, ``open`` (can still trade), or ``dead``."""
    status = _enum_str(getattr(order, "status", None))
    if status == "filled":
        return "filled"
    if status in _OPEN_STATUSES:
        return "open"
    return "dead"


def list_orders(client, *, status: QueryOrderStatus, symbol: str | None = None, side: str | None = None) -> list:
    payload = {
        "status": status,
        "limit": 100,
        "direction": Sort.DESC,
    }
    if symbol:
        payload["symbols"] = [symbol]
    if side == "sell":
        payload["side"] = OrderSide.SELL
    elif side == "buy":
        payload["side"] = OrderSide.BUY
    return list(client.get_orders(filter=GetOrdersRequest(**payload)))


def list_open_orders(client, symbol: str | None = None) -> list:
    return list_orders(client, status=QueryOrderStatus.OPEN, symbol=symbol)


def list_closed_orders(client, symbol: str, side: str | None = None) -> list:
    return list_orders(client, status=QueryOrderStatus.CLOSED, symbol=symbol, side=side)


def cancel_order(client, order_id) -> None:
    """Cancel one order. Supports current and older alpaca-py method names."""
    if hasattr(client, "cancel_order_by_id"):
        client.cancel_order_by_id(order_id)
        return
    client.cancel_order(order_id)


def cancel_open_orders(client, symbol: str) -> int:
    """Cancel every open order for ``symbol``. Returns how many were cancelled."""
    try:
        orders = list_open_orders(client, symbol)
    except Exception as exc:
        logger.warning("Could not list open orders for %s: %s", symbol, exc)
        return 0
    cancelled = 0
    for order in orders:
        try:
            cancel_order(client, order.id)
            cancelled += 1
        except Exception as exc:
            logger.warning("Could not cancel order %s for %s: %s", order.id, symbol, exc)
    return cancelled


def cancel_open_exit_orders(client, symbol: str, *, keep_ids: set[str] | None = None) -> tuple[int, int]:
    """Cancel open sell orders for ``symbol``.

    Returns ``(cancelled, still_open)``. ``still_open`` counts sell orders that
    are still working afterwards, ignoring ``keep_ids`` (our own pending exit).
    """
    keep = {str(order_id) for order_id in (keep_ids or set())}
    cancelled = 0
    for order in list_open_orders(client, symbol):
        if not is_sell_order(order):
            continue
        order_id = str(order.id)
        if order_id in keep:
            continue
        try:
            cancel_order(client, order_id)
            cancelled += 1
        except Exception as exc:
            logger.error("Failed to cancel exit order %s for %s: %s", order_id, symbol, exc)
    still_open = 0
    for order in list_open_orders(client, symbol):
        if is_sell_order(order) and str(order.id) not in keep:
            still_open += 1
    return cancelled, still_open


def cancel_sells_without_long(client, broker_qty: dict[str, int]) -> int:
    """Cancel resting sells on symbols Alpaca is not long.

    A GTC stop or a DAY sell left open after the long is gone will fill as a
    new short when ``shorting_enabled`` is on. Protective stops on a live long
    are left alone; the timed exit cancels those itself.
    """
    cancelled = 0
    for order in list_open_orders(client):
        if not is_sell_order(order):
            continue
        symbol = str(order.symbol)
        held = broker_qty.get(symbol, 0)
        if held > 0:
            continue
        try:
            cancel_order(client, order.id)
        except Exception as exc:
            logger.error(
                "Could not cancel resting sell %s for %s (Alpaca qty=%s): %s",
                order.id,
                symbol,
                held,
                exc,
            )
            continue
        cancelled += 1
        logger.warning(
            "Cancelled resting sell %s for %s (Alpaca qty=%s). "
            "Leaving it open could open or increase a short.",
            order.id,
            symbol,
            held,
        )
    return cancelled


def _row_value(row, name: str):
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def index_sell_activities(rows: list) -> dict[str, ActivityFill]:
    """Collapse FILL activities into one VWAP per sell order id."""
    grouped: dict[str, list[tuple[float, float, str]]] = {}
    for row in rows:
        if _enum_str(_row_value(row, "side")) != "sell":
            continue
        order_id = str(_row_value(row, "order_id") or "")
        price = _as_float(_row_value(row, "price"))
        qty = _as_float(_row_value(row, "qty"))
        filled_at = _as_iso(_row_value(row, "transaction_time")) or ""
        if not order_id or price is None or qty is None or qty <= 0:
            continue
        grouped.setdefault(order_id, []).append((price, qty, filled_at))

    indexed: dict[str, ActivityFill] = {}
    for order_id, parts in grouped.items():
        total_qty = sum(qty for _, qty, _ in parts)
        vwap = sum(price * qty for price, qty, _ in parts) / total_qty
        filled_at = max(parts, key=lambda part: part[2])[2]
        indexed[order_id] = ActivityFill(
            order_id=order_id,
            price=vwap,
            filled_at=filled_at,
            qty=_as_qty(total_qty),
        )
    return indexed


def load_raw_activities(client) -> list:
    """FILL activities, if the client can provide them.

    Test doubles may implement ``get_fill_activities``. The real trading
    client is read from ``GET /account/activities``. Any failure leaves the
    caller with order ``filled_avg_price`` / ``filled_at`` only.
    """
    reader = getattr(client, "get_fill_activities", None)
    if callable(reader):
        try:
            return list(reader())
        except Exception as exc:
            logger.warning("Fill activities reader failed: %s", exc)
            return []
    if not isinstance(client, TradingClient):
        return []
    try:
        payload = client.get(
            "/account/activities",
            {"activity_types": "FILL", "page_size": 100, "direction": "desc"},
        )
    except Exception as exc:
        logger.warning("Alpaca fill activities unavailable: %s", exc)
        return []
    if not isinstance(payload, list):
        logger.warning("Unexpected Alpaca activities payload: %s", type(payload).__name__)
        return []
    return payload


def fill_from_order(order, activity: ActivityFill | None = None) -> BrokerFill | None:
    """Actual sell fill. Activity price/time override the order when present."""
    if not is_sell_order(order):
        return None
    if _enum_str(getattr(order, "status", None)) != "filled":
        return None
    price = _as_float(getattr(order, "filled_avg_price", None))
    filled_at = _as_iso(getattr(order, "filled_at", None))
    qty = _as_qty(getattr(order, "filled_qty", None))
    if activity is not None:
        price = activity.price
        if activity.filled_at:
            filled_at = activity.filled_at
        if qty <= 0 and activity.qty > 0:
            qty = activity.qty
    if price is None or not filled_at or qty <= 0:
        return None
    return BrokerFill(
        order_id=str(order.id),
        price=float(price),
        filled_at=filled_at,
        qty=qty,
    )


def sell_fills(client, symbol: str, activities: dict[str, ActivityFill]) -> list[BrokerFill]:
    fills = []
    for order in list_closed_orders(client, symbol, side="sell"):
        fill = fill_from_order(order, activities.get(str(order.id)))
        if fill is not None:
            fills.append(fill)
    return fills


def latest_buy_fill_price(client, symbol: str) -> float | None:
    """Avg fill of the most recent filled buy, used when the long is already gone."""
    latest: tuple[str, float] | None = None
    for order in list_closed_orders(client, symbol, side="buy"):
        if _enum_str(getattr(order, "status", None)) != "filled":
            continue
        price = _as_float(getattr(order, "filled_avg_price", None))
        filled_at = _as_iso(getattr(order, "filled_at", None)) or ""
        if price is None:
            continue
        if latest is None or filled_at >= latest[0]:
            latest = (filled_at, price)
    if latest is None:
        return None
    return latest[1]


def fetch_order(client, order_id: str):
    getter = getattr(client, "get_order_by_id", None)
    if not callable(getter):
        logger.error("Alpaca client cannot fetch order %s.", order_id)
        return None
    try:
        return getter(order_id)
    except Exception as exc:
        logger.warning("Could not fetch order %s: %s", order_id, exc)
        return None
