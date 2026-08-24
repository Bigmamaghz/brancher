from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from src.auth import generate_api_key
from src.config import LOOP_INTERVAL_SEC, load_settings
from src.executor import Executor
from src.poll import poll_all
from src.registry import enabled_bots
from src.telegram import TelegramClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def cmd_keys(args: argparse.Namespace) -> int:
    key = generate_api_key(args.bot)
    env_name = args.bot.upper().replace("-", "_") + "_API_KEY"
    print(f"{env_name}={key}")
    print()
    print("Brancher .env:")
    print(f"  {env_name}={key}")
    print()
    print("Author .env:")
    print(f"  BRANCHER_API_KEY={key}")
    return 0


def cmd_ping(args: argparse.Namespace) -> int:
    settings = load_settings()
    tg = TelegramClient(settings)
    ok = tg.ping(dry_run=args.dry_run)
    print("ping sent" if ok else "ping failed (check TELEGRAM_* env vars)")
    return 0 if ok else 1


def cmd_poll(args: argparse.Namespace) -> int:
    load_settings()  # load .env so bot API keys resolve
    bots = enabled_bots()
    results = poll_all(bots)
    for r in results:
        status = "error" if r.error else f"{len(r.signals)} signals"
        print(f"  {r.bot_id}: {status}")
        if r.error:
            print(f"    {r.error}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = load_settings()
    executor = Executor(settings, dry_run=True)
    status = executor.status()
    print(json.dumps(status, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings()
    executor = Executor(settings, dry_run=args.dry_run)

    if args.once:
        executor.run_cycle()
        return 0

    logger.info("Starting Brancher loop (interval=%ds, dry_run=%s)", LOOP_INTERVAL_SEC, args.dry_run)
    while True:
        try:
            executor.run_cycle()
        except Exception:
            logger.exception("Cycle failed")
        time.sleep(LOOP_INTERVAL_SEC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brancher", description="Universal portfolio controller")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run full portfolio loop")
    run_p.add_argument("--dry-run", action="store_true", help="No Alpaca orders")
    run_p.add_argument("--once", action="store_true", help="Run one cycle then exit")
    run_p.set_defaults(func=cmd_run)

    sub.add_parser("poll", help="Poll all bots once").set_defaults(func=cmd_poll)
    sub.add_parser("status", help="Show portfolio status").set_defaults(func=cmd_status)

    ping_p = sub.add_parser("ping", help="Send test Telegram message")
    ping_p.add_argument("--dry-run", action="store_true")
    ping_p.set_defaults(func=cmd_ping)

    keys_p = sub.add_parser("keys", help="Generate Author API key")
    keys_p.add_argument("--bot", required=True, help="Bot ID (e.g. financials-xlf)")
    keys_p.set_defaults(func=cmd_keys)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
