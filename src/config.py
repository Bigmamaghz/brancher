from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

LIVE_HOSTS = ("api.alpaca.markets",)
PAPER_HOSTS = ("paper-api.alpaca.markets",)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "portfolio"
BOTS_DATA_DIR = DATA_DIR / "bots"
BOOK_PATH = DATA_DIR / "book.json"
BOTS_YAML = REPO_ROOT / "config" / "bots.yaml"
LOOP_INTERVAL_SEC = 900  # 15 minutes


class PaperOnlyError(RuntimeError):
    """Raised when a live Alpaca endpoint is detected."""


@dataclass(frozen=True)
class Settings:
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str | None
    telegram_bot_token: str
    telegram_chat_id: str
    paper_qty: int
    paper_qty_max_mult: int
    max_open_positions: int
    max_opens_per_day: int
    min_hit: float
    telegram_news_only: bool
    telegram_eod: bool


def _validate_paper_url(url: str | None) -> str | None:
    if not url:
        return None
    lower = url.lower()
    if any(host in lower for host in PAPER_HOSTS):
        return url
    for host in LIVE_HOSTS:
        if host in lower:
            raise PaperOnlyError(
                f"Refusing live Alpaca URL ({host}). "
                "Brancher is paper-only. Use paper-api.alpaca.markets or omit ALPACA_BASE_URL."
            )
    return url


def load_settings(env_path: Path | None = None) -> Settings:
    if env_path:
        load_dotenv(env_path)
    else:
        load_dotenv(REPO_ROOT / ".env")

    base_url = os.getenv("ALPACA_BASE_URL")
    _validate_paper_url(base_url)

    return Settings(
        alpaca_api_key=os.getenv("ALPACA_API_KEY", ""),
        alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
        alpaca_base_url=base_url,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        paper_qty=int(os.getenv("ALPACA_PAPER_QTY", "1")),
        paper_qty_max_mult=int(os.getenv("ALPACA_PAPER_QTY_MAX_MULT", "4")),
        max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS", "10")),
        max_opens_per_day=int(os.getenv("MAX_OPENS_PER_DAY", "5")),
        min_hit=float(os.getenv("MIN_HIT", "0.75")),
        telegram_news_only=os.getenv("TELEGRAM_NEWS_ONLY", "1").lower() in ("1", "true", "yes"),
        telegram_eod=os.getenv("TELEGRAM_EOD", "0").lower() in ("1", "true", "yes"),
    )


def ensure_data_dirs() -> None:
    BOTS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (REPO_ROOT / "data" / "logs").mkdir(parents=True, exist_ok=True)
