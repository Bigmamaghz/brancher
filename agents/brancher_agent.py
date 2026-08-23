"""Brancher agent — 15-minute portfolio loop."""

from __future__ import annotations

import logging
import time

from src.config import LOOP_INTERVAL_SEC, load_settings
from src.executor import Executor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main(dry_run: bool = False) -> None:
    settings = load_settings()
    executor = Executor(settings, dry_run=dry_run)
    logger.info("Brancher agent started (interval=%ds)", LOOP_INTERVAL_SEC)

    while True:
        try:
            executor.run_cycle()
        except Exception:
            logger.exception("Cycle failed")
        time.sleep(LOOP_INTERVAL_SEC)


if __name__ == "__main__":
    main()
