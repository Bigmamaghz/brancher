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


def cmd_update(args: argparse.Namespace) -> int:
    """Poll all Authors and Telegram you a per-bot UPDATE digest."""
    settings = load_settings()
    executor = Executor(settings, dry_run=args.dry_run)
    results = executor.send_updates_only()
    for r in results:
        status = "error" if r.error else f"{len(r.signals)} signals"
        print(f"  {r.bot_id}: {status}")
        if r.error:
            print(f"    {r.error}")
    print("updates sent to Telegram" if any(not r.error for r in results) or results else "done")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Show which Authors are OK vs FAIL, then scan localhost for real ports."""
    load_settings()
    bots = enabled_bots()
    results = poll_all(bots)
    from src.news import summarize_health

    rows = summarize_health(results)
    print("")
    print("Brancher doctor — Author health")
    print("-" * 60)
    failed = 0
    for row in rows:
        if row["ok"]:
            print(f"  OK   {row['bot_name']:30} {row['signals']} signals")
        else:
            failed += 1
            print(f"  FAIL {row['bot_name']:30} {row['error']}")
    print("-" * 60)

    # Built-in discovery (same machine terminal)
    from src.discover import discover, format_discovery_report
    from src.registry import load_bots as all_bots

    probes = discover(bots=all_bots())
    print(format_discovery_report(probes, all_bots()))

    if failed:
        print(f"{failed} Author(s) FAILING.")
        print("Run:  python3 -m src.cli find --fix   # auto-remap ports if Authors are on wrong ports")
        return 1
    print("All Authors OK.")
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    """Scan 127.0.0.1 ports, identify Authors, optionally rewrite bots.yaml."""
    load_settings()
    from src.discover import apply_discovered_ports, discover, format_discovery_report
    from src.registry import load_bots

    bots = load_bots()
    probes = discover(bots=bots)
    print(format_discovery_report(probes, bots))

    if args.fix:
        changes = apply_discovered_ports(probes)
        if changes:
            print("Applied fixes to config/bots.yaml:")
            for c in changes:
                print(f"  {c}")
            print("Re-run: python3 -m src.cli doctor")
        else:
            print("No auto-fix available — missing Authors are not listening on this Mac.")
            return 1
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
    sub.add_parser("doctor", help="Show which Authors are OK vs FAIL + scan ports").set_defaults(func=cmd_doctor)

    find_p = sub.add_parser("find", help="Scan localhost ports and identify Authors")
    find_p.add_argument("--fix", action="store_true", help="Rewrite bots.yaml to match discovered ports")
    find_p.set_defaults(func=cmd_find)

    update_p = sub.add_parser("update", help="Poll Authors and Telegram status updates (manual only)")
    update_p.add_argument("--dry-run", action="store_true")
    update_p.set_defaults(func=cmd_update)

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
