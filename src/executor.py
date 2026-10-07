from __future__ import annotations

import logging
from datetime import datetime, timezone
from collections import defaultdict

from src.alpaca import get_equity, get_open_positions, get_trading_client, submit_buy
from src.book import Book, load_book, make_position, save_book
from src.config import Settings, ensure_data_dirs
from src.joe_gate import joe_check
from src.exits import (
    ReconcileEvent,
    SellResult,
    reconcile_book as sync_book_with_broker,
    sell_due_position,
)
from src.merge import MergedSignal, SkippedSignal, merge_signals
from src.news import NewsItem, detect_news, summarize_health
from src.poll import PollResult, poll_all
from src.registry import BotConfig, enabled_bots
from src.risk import can_open_position, check_eligible, should_enter
from src.research_feedback import feedback_report
from src.shadow import bench_reason, record_closed
from src.schedule import (
    is_eod_time,
    is_enter_today,
    is_sell_today,
    notices_due,
    today_et_str,
)
from src.sizing import qty_for_hit
from src.telegram import TelegramClient, format_dates, format_message, format_signal_line

logger = logging.getLogger(__name__)


class Executor:
    def __init__(self, settings: Settings, dry_run: bool = False):
        self.settings = settings
        self.dry_run = dry_run
        self.telegram = TelegramClient(settings)
        self._client = None

    @property
    def client(self):
        if self._client is None and not self.dry_run:
            if self.settings.alpaca_api_key and self.settings.alpaca_secret_key:
                self._client = get_trading_client(self.settings)
        return self._client

    def run_cycle(self, bots: list[BotConfig] | None = None) -> None:
        ensure_data_dirs()
        bots = bots or enabled_bots()
        today = today_et_str()
        book = load_book()

        results = poll_all(bots)
        self._log_poll_errors(results)

        # Always refresh snapshots (silent) so we don't re-notify later
        news, book.signal_snapshots, book.bot_online = detect_news(
            results,
            book.signal_snapshots,
            book.bot_online,
            self.settings.min_hit,
        )

        # Quiet modes — never spam status/FAIL/UPDATE/SKIP/ADVANCE on a timer.
        # NEWS = real signal changes only (NEW / URGENCY / HIT_UP / ENTER_TODAY).
        # ENTER/SELL always text (actual paper trades).
        if self.settings.telegram_news_only and not self.settings.telegram_trades_only:
            self._send_news(news, book)
        elif not self.settings.telegram_news_only and not self.settings.telegram_trades_only:
            # Legacy noisy mode (explicitly opt-in by setting both flags to 0)
            winners, losers = merge_signals(results)
            self._send_skip_for_losers(losers, book)
            self._send_advance_notices(winners, book, today)
            self._send_enter_today_notices(winners, book, today)

        winners, _ = merge_signals(results)
        # Sync the book with Alpaca before any timed exit. A stop that already
        # filled must be booked here, or the exit below would sell a flat name.
        if self.client:
            self.reconcile_book(book)
        self._process_sells(book, today)
        self._process_enters(winners, book, today)

        if (
            self.settings.telegram_eod
            and is_eod_time()
            and not book.eod_already_sent(today)
        ):
            self._send_eod(book, results, today)

        save_book(book)

    def _send_news(self, items: list[NewsItem], book: Book) -> None:
        for item in items:
            label = f"news:{item.kind}:{item.signal.id if item.signal else item.bot_id}"
            if book.notice_already_sent(label, "sent"):
                continue
            if item.signal:
                sig = item.signal
                msg = format_message(
                    kind="NEWS",
                    source=item.bot_name,
                    signal_line=format_signal_line(
                        sig.ticker, sig.event_type, sig.side, sig.hit
                    ),
                    action=f"{item.kind} · {item.detail}",
                    dates=format_dates(sig.enter_on, sig.close_on),
                )
            else:
                msg = format_message(
                    kind="NEWS",
                    source=item.bot_name,
                    action=f"{item.kind} · {item.detail}",
                )
            self.telegram.send(msg, dry_run=self.dry_run)
            book.mark_notice_sent(label, "sent")

    def send_updates_only(self, bots: list[BotConfig] | None = None) -> list[PollResult]:
        """On-demand poll + Telegram STATUS digest (python -m src.cli update)."""
        ensure_data_dirs()
        bots = bots or enabled_bots()
        results = poll_all(bots)
        self._log_poll_errors(results)
        self._send_bot_updates(results)
        return results

    def _log_poll_errors(self, results: list[PollResult]) -> None:
        for r in results:
            if r.error:
                logger.warning("Poll error for %s: %s", r.bot_id, r.error)

    def _send_bot_updates(self, results: list[PollResult]) -> None:
        today = today_et_str()
        for r in results:
            if r.error:
                self.telegram.bot_update(
                    bot_name=r.bot_name or r.bot_id,
                    status="offline / poll failed",
                    detail=r.error[:200],
                    dry_run=self.dry_run,
                )
                continue

            signals = r.signals
            soon = [s for s in signals if s.urgency in ("soon", "in_play") or s.enter_on == today]
            upcoming = sorted(
                [s for s in signals if s.eligible],
                key=lambda s: (-s.hit, s.enter_on),
            )[:5]
            top = ", ".join(
                f"{s.ticker} {int(round(s.hit * 100))}% enter={s.enter_on}"
                for s in upcoming
            ) or "none"
            detail = (
                f"signals={len(signals)} soon/in_play/today={len(soon)} "
                f"top=[{top}]"
            )
            self.telegram.bot_update(
                bot_name=r.bot_name or r.bot_id,
                status=f"online · {len(signals)} signals",
                detail=detail,
                dry_run=self.dry_run,
            )

    def _send_skip_for_losers(self, losers: list[SkippedSignal], book: Book) -> None:
        for skip in losers:
            sig = skip.signal
            label = f"skip:beaten:{skip.winner_bot}"
            if book.notice_already_sent(sig.id, label):
                continue
            msg = format_message(
                kind="SKIP",
                source=skip.bot_name,
                signal_line=format_signal_line(sig.ticker, sig.event_type, sig.side, sig.hit),
                action=f"SKIP {skip.reason}",
                detail=f"winner={skip.winner_bot} hit={skip.winner_hit:.0%}",
                dates=format_dates(sig.enter_on, sig.close_on),
            )
            self.telegram.send(msg, dry_run=self.dry_run)
            book.mark_notice_sent(sig.id, label)

    def _send_advance_notices(self, winners: list[MergedSignal], book: Book, today: str) -> None:
        for merged in winners:
            sig = merged.signal
            for label in notices_due(sig.enter_on):
                if book.notice_already_sent(sig.id, label):
                    continue
                msg = format_message(
                    kind="ADVANCE",
                    source=merged.bot_name,
                    signal_line=format_signal_line(sig.ticker, sig.event_type, sig.side, sig.hit),
                    action=f"ADVANCE {label} before enter",
                    dates=format_dates(sig.enter_on, sig.close_on),
                )
                self.telegram.send(msg, dry_run=self.dry_run)
                book.mark_notice_sent(sig.id, label)

    def _send_enter_today_notices(
        self,
        winners: list[MergedSignal],
        book: Book,
        today: str,
    ) -> None:
        for merged in winners:
            sig = merged.signal
            if not is_enter_today(sig.enter_on):
                continue
            label = "enter_today"
            if book.notice_already_sent(sig.id, label):
                continue
            msg = format_message(
                kind="ADVANCE",
                source=merged.bot_name,
                signal_line=format_signal_line(sig.ticker, sig.event_type, sig.side, sig.hit),
                action="ENTER TODAY",
                dates=format_dates(sig.enter_on, sig.close_on),
            )
            self.telegram.send(msg, dry_run=self.dry_run)
            book.mark_notice_sent(sig.id, label)

    def reconcile_book(self, book: Book) -> None:
        """Book broker-closed longs and flag positions the book does not know."""
        if self.client is None:
            return
        for event in sync_book_with_broker(self.client, book):
            self._announce_reconcile(book, event)

    def _process_sells(self, book: Book, today: str) -> None:
        to_close = [p for p in list(book.positions) if is_sell_today(p.close_on)]
        for pos in to_close:
            if not self.client:
                # No broker order is sent. Recording the planned close is what
                # dry-run has always done; live sells wait for a fill.
                record_closed(
                    book,
                    pos.ticker,
                    pos.bot_id,
                    pos.signal_id,
                    pos.enter_on,
                    pos.close_on,
                    None,
                    None,
                    datetime.now(timezone.utc).isoformat(),
                )
                book.close_position(pos.ticker)
                self.telegram.send(
                    format_message(
                        kind="SELL",
                        source=pos.bot_name,
                        signal_line=f"{pos.ticker}",
                        action=f"SELL sell qty={pos.qty}",
                        detail="dry-run",
                        dates=f"SELL: {pos.close_on} next session",
                    ),
                    dry_run=self.dry_run,
                )
                continue

            result = sell_due_position(self.client, book, pos)
            self._announce_sell(pos, result)

        # SELL TODAY — news mode only if closing today (handled by SELL above)
        if self.settings.telegram_news_only:
            return
        for pos in book.positions:
            if pos.close_on != today:
                continue
            label = "sell_today"
            if book.notice_already_sent(pos.signal_id, label):
                continue
            msg = format_message(
                kind="ADVANCE",
                source=pos.bot_name,
                signal_line=f"{pos.ticker}",
                action="SELL TODAY",
                dates=f"SELL: {pos.close_on} next session",
            )
            self.telegram.send(msg, dry_run=self.dry_run)
            book.mark_notice_sent(pos.signal_id, label)

    def _process_enters(
        self,
        winners: list[MergedSignal],
        book: Book,
        today: str,
    ) -> None:
        open_tickers = book.open_tickers()

        for merged in winners:
            sig = merged.signal

            if sig.ticker in open_tickers:
                continue

            bench = bench_reason(book, sig.id)
            if bench:
                if not self.settings.telegram_news_only:
                    self._send_skip_once(merged, book, bench)
                continue

            skip_reason = check_eligible(sig, self.settings)
            if skip_reason:
                if not self.settings.telegram_news_only:
                    self._send_skip_once(merged, book, skip_reason)
                continue

            if not should_enter(merged, today):
                continue

            # joe's gate (paper veto) — only reached after every hard rule passed.
            # joe is research-only: he never orders, never texts. Infra failure
            # inside joe_check fails open; the stack never stalls on joe.
            if self.settings.joe_veto:
                allow, jreason = joe_check(sig)
                if not allow:
                    logger.info("joe veto %s: %s", sig.id, jreason)
                    if not self.settings.telegram_news_only:
                        self._send_skip_once(merged, book, f"joe: {jreason}")
                    continue

            cap_reason = can_open_position(
                len(book.positions),
                book.opens_today_count(today),
                self.settings,
            )
            if cap_reason:
                if not self.settings.telegram_news_only:
                    self._send_skip_once(merged, book, cap_reason)
                continue

            qty = qty_for_hit(
                sig.hit,
                self.settings.paper_qty,
                self.settings.paper_qty_max_mult,
                self.settings.min_hit,
            )
            if qty <= 0:
                if not self.settings.telegram_news_only:
                    self._send_skip_once(merged, book, "qty=0 after sizing")
                continue

            order_id = "dry-run"
            if self.client:
                try:
                    order_id = submit_buy(self.client, sig.ticker, qty)
                except Exception as exc:
                    logger.error("Buy failed for %s: %s", sig.ticker, exc)
                    if not self.settings.telegram_news_only:
                        self._send_skip_once(merged, book, f"order failed: {exc}")
                    continue

            position = make_position(
                ticker=sig.ticker,
                qty=qty,
                bot_id=merged.bot_id,
                bot_name=merged.bot_name,
                signal_id=sig.id,
                enter_on=sig.enter_on,
                close_on=sig.close_on,
            )
            book.record_open(position, today)
            open_tickers.add(sig.ticker)

            msg = format_message(
                kind="ENTER",
                source=merged.bot_name,
                signal_line=format_signal_line(sig.ticker, sig.event_type, sig.side, sig.hit),
                action=f"ENTER buy qty={qty}",
                detail=f"order={order_id}" if not self.dry_run else "dry-run",
                dates=format_dates(sig.enter_on, sig.close_on),
            )
            self.telegram.send(msg, dry_run=self.dry_run)

    def _announce_sell(self, pos, result: SellResult) -> None:
        if result.kind == "submitted":
            action = f"SELL submitted qty={result.qty}"
            detail = f"order={result.order_id}; close is booked only after the fill"
        elif result.kind == "filled":
            action = f"SELL filled qty={result.qty} @ {result.fill_price}"
            detail = f"order={result.order_id}; fill time {result.fill_time}"
        elif result.kind == "reconciled":
            action = f"SELL reconciled qty={pos.qty} @ {result.fill_price}"
            detail = f"no long left at Alpaca; fill time {result.fill_time}"
        elif result.kind in ("skip", "wait"):
            return
        else:
            logger.error("Unhandled sell result %s", result.kind)
            return
        self.telegram.send(
            format_message(
                kind="SELL",
                source=pos.bot_name,
                signal_line=f"{pos.ticker}",
                action=action,
                detail=detail,
                dates=f"SELL: {pos.close_on} next session",
            ),
            dry_run=self.dry_run,
        )

    def _announce_reconcile(self, book: Book, event: ReconcileEvent) -> None:
        if event.kind == "closed":
            self.telegram.send(
                format_message(
                    kind="SELL",
                    source=event.bot_name,
                    signal_line=event.symbol,
                    action=f"SELL reconciled qty={event.qty} @ {event.fill_price}",
                    detail=f"broker position already closed; fill time {event.fill_time}",
                    dates=f"SELL: {event.close_on}",
                ),
                dry_run=self.dry_run,
            )
            return
        if event.kind == "unexpected_short":
            action = f"UNEXPECTED SHORT {event.symbol} qty={event.qty}"
            detail = "Alpaca holds a short the book does not know. Brancher will not trade it."
        elif event.kind == "unexpected_long":
            action = f"UNEXPECTED LONG {event.symbol} qty={event.qty}"
            detail = "Alpaca holds shares the book does not know. Brancher will not trade them."
        else:
            logger.error("Unhandled reconcile event %s", event.kind)
            return
        label = f"{event.kind}:{event.qty}"
        key = f"reconcile:{event.symbol}"
        if book.notice_already_sent(key, label):
            return
        self.telegram.send(
            format_message(
                kind="RECONCILE",
                source="Brancher",
                signal_line=event.symbol,
                action=action,
                detail=detail,
            ),
            dry_run=self.dry_run,
        )
        book.mark_notice_sent(key, label)

    def _send_skip_once(self, merged: MergedSignal, book: Book, reason: str) -> None:
        sig = merged.signal
        label = f"skip:{reason}"
        if book.notice_already_sent(sig.id, label):
            return
        msg = format_message(
            kind="SKIP",
            source=merged.bot_name,
            signal_line=format_signal_line(sig.ticker, sig.event_type, sig.side, sig.hit),
            action=f"SKIP {reason}",
            dates=format_dates(sig.enter_on, sig.close_on),
        )
        self.telegram.send(msg, dry_run=self.dry_run)
        book.mark_notice_sent(sig.id, label)

    def _send_eod(
        self,
        book: Book,
        results: list[PollResult],
        today: str,
    ) -> None:
        equity = 0.0
        if self.client:
            try:
                equity = get_equity(self.client)
            except Exception as exc:
                logger.warning("Could not fetch equity: %s", exc)

        opened_today = book.opens_today.get(today, 0)
        open_positions = book.positions

        per_bot: dict[str, int] = defaultdict(int)
        for p in open_positions:
            per_bot[p.bot_name] += 1

        bot_lines = ", ".join(f"{name}={count}" for name, count in sorted(per_bot.items()))
        if not bot_lines:
            bot_lines = "none"

        detail = (
            f"equity=${equity:,.2f} opened={opened_today} "
            f"open={len(open_positions)} bots=[{bot_lines}] "
            f"research_feedback={feedback_report()['count']}"
        )

        msg = format_message(
            kind="EOD",
            source="Brancher",
            action="end of day summary",
            detail=detail,
        )
        self.telegram.send(msg, dry_run=self.dry_run)
        book.mark_eod_sent(today)

    def doctor(self) -> list[dict]:
        """Poll every Author and return OK/FAIL rows (no Telegram)."""
        ensure_data_dirs()
        bots = enabled_bots()
        results = poll_all(bots)
        return summarize_health(results)

    def status(self) -> dict:
        book = load_book()
        today = today_et_str()
        status = {
            "today": today,
            "open_positions": len(book.positions),
            "opens_today": book.opens_today_count(today),
            "positions": [p.to_dict() for p in book.positions],
            # Read-only research context. This is intentionally not passed to
            # merge/risk/order code and cannot affect signal eligibility.
            "research_feedback": feedback_report(),
        }
        if self.client:
            try:
                status["equity"] = get_equity(self.client)
                status["alpaca_positions"] = get_open_positions(self.client)
            except Exception as exc:
                status["alpaca_error"] = str(exc)
        return status
