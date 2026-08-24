from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import BOOK_PATH, ensure_data_dirs


@dataclass
class Position:
    ticker: str
    qty: int
    bot_id: str
    bot_name: str
    signal_id: str
    enter_on: str
    close_on: str
    opened_at: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Position:
        return cls(**d)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Book:
    positions: list[Position] = field(default_factory=list)
    opens_today: dict[str, int] = field(default_factory=dict)
    notices_sent: dict[str, list[str]] = field(default_factory=dict)
    eod_sent: dict[str, bool] = field(default_factory=dict)
    signal_snapshots: dict[str, dict[str, dict]] = field(default_factory=dict)
    bot_online: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Book:
        return cls(
            positions=[Position.from_dict(p) for p in d.get("positions", [])],
            opens_today=d.get("opens_today", {}),
            notices_sent=d.get("notices_sent", {}),
            eod_sent=d.get("eod_sent", {}),
            signal_snapshots=d.get("signal_snapshots", {}),
            bot_online=d.get("bot_online", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "positions": [p.to_dict() for p in self.positions],
            "opens_today": self.opens_today,
            "notices_sent": self.notices_sent,
            "eod_sent": self.eod_sent,
            "signal_snapshots": self.signal_snapshots,
            "bot_online": self.bot_online,
        }

    def open_tickers(self) -> set[str]:
        return {p.ticker for p in self.positions}

    def opens_today_count(self, today_str: str) -> int:
        return self.opens_today.get(today_str, 0)

    def record_open(self, position: Position, today_str: str) -> None:
        self.positions.append(position)
        self.opens_today[today_str] = self.opens_today.get(today_str, 0) + 1

    def close_position(self, ticker: str) -> Position | None:
        for i, p in enumerate(self.positions):
            if p.ticker == ticker:
                return self.positions.pop(i)
        return None

    def notice_already_sent(self, signal_id: str, label: str) -> bool:
        return label in self.notices_sent.get(signal_id, [])

    def mark_notice_sent(self, signal_id: str, label: str) -> None:
        sent = self.notices_sent.setdefault(signal_id, [])
        if label not in sent:
            sent.append(label)

    def eod_already_sent(self, today_str: str) -> bool:
        return self.eod_sent.get(today_str, False)

    def mark_eod_sent(self, today_str: str) -> None:
        self.eod_sent[today_str] = True


def load_book(path: Path | None = None) -> Book:
    ensure_data_dirs()
    p = path or BOOK_PATH
    if not p.exists():
        return Book()
    with open(p) as f:
        return Book.from_dict(json.load(f))


def save_book(book: Book, path: Path | None = None) -> None:
    ensure_data_dirs()
    p = path or BOOK_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(book.to_dict(), f, indent=2)


def make_position(
    ticker: str,
    qty: int,
    bot_id: str,
    bot_name: str,
    signal_id: str,
    enter_on: str,
    close_on: str,
) -> Position:
    return Position(
        ticker=ticker,
        qty=qty,
        bot_id=bot_id,
        bot_name=bot_name,
        signal_id=signal_id,
        enter_on=enter_on,
        close_on=close_on,
        opened_at=datetime.now(timezone.utc).isoformat(),
    )
