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


def get_equity(client) -> float:
    account = client.get_account()
    return float(account.equity)


def get_open_positions(client) -> dict[str, int]:
    positions = client.get_all_positions()
    return {p.symbol: int(float(p.qty)) for p in positions}
