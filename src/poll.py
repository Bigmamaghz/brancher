from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from src.config import BOTS_DATA_DIR
from src.registry import BotConfig


@dataclass
class Signal:
    id: str
    ticker: str
    side: str
    hit: float
    n: int
    event_type: str
    enter_on: str
    close_on: str
    eligible: bool
    urgency: str
    sent_at: str
    pattern_id: str | None = None
    sec_url: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Signal:
        return cls(
            id=d["id"],
            ticker=d["ticker"].upper(),
            side=d["side"],
            hit=float(d["hit"]),
            n=int(d["n"]),
            event_type=d["event_type"],
            enter_on=d["enter_on"],
            close_on=d["close_on"],
            eligible=bool(d.get("eligible", True)),
            urgency=d.get("urgency", "scheduled"),
            sent_at=d.get("sent_at", ""),
            pattern_id=d.get("pattern_id"),
            sec_url=d.get("sec_url"),
        )


@dataclass
class PollResult:
    bot_id: str
    bot_name: str
    updated_at: str
    signals: list[Signal]
    error: str | None = None


def _bot_data_dir(bot_id: str) -> Path:
    d = BOTS_DATA_DIR / bot_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log_poll(bot: BotConfig, result: PollResult, raw: dict | None) -> None:
    bot_dir = _bot_data_dir(bot.id)
    now = datetime.now(timezone.utc).isoformat()

    last_seen = {
        "bot_id": result.bot_id,
        "bot_name": result.bot_name,
        "updated_at": result.updated_at,
        "polled_at": now,
        "signal_count": len(result.signals),
        "error": result.error,
    }
    (bot_dir / "last_seen.json").write_text(json.dumps(last_seen, indent=2))

    if raw and result.signals:
        with open(bot_dir / "signals.jsonl", "a") as f:
            record = {"polled_at": now, "signals": [s.__dict__ for s in result.signals]}
            f.write(json.dumps(record) + "\n")


def poll_bot(bot: BotConfig, timeout: float = 30.0) -> PollResult:
    api_key = bot.api_key()
    if not api_key:
        result = PollResult(
            bot_id=bot.id,
            bot_name=bot.name,
            updated_at="",
            signals=[],
            error=f"Missing env var {bot.api_key_env}",
        )
        _log_poll(bot, result, None)
        return result

    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(bot.signals_url, headers=headers)
            resp.raise_for_status()
            raw = resp.json()
    except Exception as exc:
        result = PollResult(
            bot_id=bot.id,
            bot_name=bot.name,
            updated_at="",
            signals=[],
            error=str(exc),
        )
        _log_poll(bot, result, None)
        return result

    signals = [Signal.from_dict(s) for s in raw.get("signals", [])]
    result = PollResult(
        bot_id=raw.get("bot_id", bot.id),
        bot_name=raw.get("bot_name", bot.name),
        updated_at=raw.get("updated_at", ""),
        signals=signals,
    )
    _log_poll(bot, result, raw)
    return result


def poll_all(bots: list[BotConfig]) -> list[PollResult]:
    return [poll_bot(b) for b in bots]
