from __future__ import annotations

import httpx

from src.config import Settings

DISCLAIMER = "Paper money. Research only. Not a buy."


def format_message(
    kind: str,
    source: str,
    signal_line: str = "",
    action: str = "",
    detail: str = "",
    dates: str = "",
) -> str:
    lines = [
        "[Brancher · Portfolio]",
        f"KIND: {kind}",
        f"SOURCE: {source}",
    ]
    if signal_line:
        lines.append(f"SIGNAL: {signal_line}")
    if action:
        lines.append(f"ACTION: {action}")
    if detail:
        lines.append(f"DETAIL: {detail}")
    if dates:
        lines.append(dates)
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_signal_line(ticker: str, event_type: str, side: str, hit: float) -> str:
    pct = int(round(hit * 100))
    return f"{ticker} {event_type} {side} {pct}%"


def format_dates(enter_on: str, close_on: str) -> str:
    return f"ENTER: {enter_on} close · SELL: {close_on} next session"


class TelegramClient:
    def __init__(self, settings: Settings):
        self._token = settings.telegram_bot_token
        self._chat_id = settings.telegram_chat_id
        self._base = f"https://api.telegram.org/bot{self._token}"

    def send(self, text: str, dry_run: bool = False) -> bool:
        if dry_run and not self._token:
            print(text)
            return True
        if not self._token or not self._chat_id:
            print(f"[telegram skipped] {text}")
            return False
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{self._base}/sendMessage",
                json={"chat_id": self._chat_id, "text": text},
            )
            resp.raise_for_status()
            return True

    def ping(self, dry_run: bool = False) -> bool:
        msg = format_message(
            kind="PING",
            source="Brancher",
            action="ping ok",
            detail="connectivity test",
        )
        return self.send(msg, dry_run=dry_run)

    def bot_update(
        self,
        bot_name: str,
        status: str,
        detail: str,
        dry_run: bool = False,
    ) -> bool:
        msg = format_message(
            kind="UPDATE",
            source=bot_name,
            action=status,
            detail=detail,
        )
        return self.send(msg, dry_run=dry_run)
