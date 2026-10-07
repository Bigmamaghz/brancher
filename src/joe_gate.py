"""joe_gate.py — fail-closed entry gate for Brancher paper orders.

Joe is research-only: he never places orders and never texts. When JOE_VETO=1
the executor reaches this gate only after the daily open cap. Any no below is
a skip: logged, and never an order.

Order inside the gate:
  1. Pattern inventory, read from disk on every check (no cache).
  2. Stop and target come only from levels.playbook_levels. Joe's verdict is a
     bare word; digits in that word are ignored and the mismatch is logged.
  3. Joe consult, 10s timeout, fail closed.

Approve words:
  Brancher cannot open a short. risk.check_eligible rejects every side except
  UP before this gate, so the live rule is TAKE or BUY.
  If a short signal is handed in anyway, the previous router is kept for
  direction: TAKE still approves, SELL or SHORT approves a short, BUY does not.
  WAIT, HOLD, SKIP, APPROVE, an empty verdict, and anything else do not.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import httpx

from src.config import DEFAULT_JOE_URL, DEFAULT_PATTERN_INVENTORY, Settings
from src.decision_log import log_entry_decision
from src.levels import playbook_levels

logger = logging.getLogger(__name__)

CONSULT_TIMEOUT = 10.0

_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")
_WORD = re.compile(r"[A-Za-z]+")
_SHORT_SIDES = frozenset({"short", "sell", "down"})
_LONG_APPROVE = frozenset({"TAKE", "BUY"})
_SHORT_APPROVE = frozenset({"TAKE", "SELL", "SHORT"})


def pattern_key(name: str) -> str:
    """Drop one trailing -YYYY-MM-DD so dated and undated names compare equal."""
    return _DATE_SUFFIX.sub("", (name or "").strip())


def signal_pattern(signal) -> str:
    pattern_id = getattr(signal, "pattern_id", None)
    if pattern_id:
        return str(pattern_id)
    return str(getattr(signal, "event_type", "") or "")


def _is_short(side: str) -> bool:
    return (side or "").strip().lower() in _SHORT_SIDES


def _ticker_from_cell(cell: str) -> str | None:
    """First word of the ticker cell. A cell that starts with 'sector' is not a ticker."""
    text = (cell or "").strip()
    if not text or text.lower().startswith("sector"):
        return None
    return text.split()[0].upper()


def _parse_inventory_line(line: str) -> tuple[str, str | None, str] | None:
    text = line.strip()
    if not text.startswith("-"):
        return None
    parts = [part.strip() for part in text[1:].split("|")]
    if len(parts) < 4:
        return None
    pattern = parts[0]
    status = parts[-1].strip().strip("*").lower()
    if not pattern or not status:
        return None
    return pattern, _ticker_from_cell(parts[1]), status


def inventory_status(path: Path, pattern: str, ticker: str) -> str:
    """Fresh read. Returns active, another status, absent, missing, or unparseable.

    active is the only status the gate treats as a pass. Sector rows never match.
    """
    if not path.is_file():
        return "missing"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "unparseable"

    rows: list[tuple[str, str | None, str]] = []
    for line in text.splitlines():
        parsed = _parse_inventory_line(line)
        if parsed is not None:
            rows.append(parsed)
    if not rows and "pattern inventory" not in text.lower():
        return "unparseable"

    want_pattern = pattern_key(pattern)
    want_ticker = (ticker or "").split()[0].upper()
    found: list[str] = []
    for row_pattern, row_ticker, status in rows:
        if row_ticker is None or row_ticker != want_ticker:
            continue
        if pattern_key(row_pattern) != want_pattern:
            continue
        found.append(status)
    if not found:
        return "absent"
    if all(status == "active" for status in found):
        return "active"
    return next(status for status in found if status != "active")


def _levels_note(signal, verdict: str) -> str:
    """Code stops only. A fill price is frozen later by the same playbook formula.

    When the signal already carries an entry and ATR, compute them here so a
    number in Joe's text cannot become the stop or target.
    """
    note = "sl/tp from playbook_levels only"
    if re.search(r"\d", verdict or ""):
        note += "; joe text has levels and is ignored"
    entry = getattr(signal, "entry_price", None)
    atr = getattr(signal, "atr", None)
    if not isinstance(entry, (int, float)) or entry <= 0:
        return note
    if not isinstance(atr, (int, float)) or atr <= 0:
        return note
    side = "SHORT" if _is_short(getattr(signal, "side", "") or "") else "BUY"
    stop, target = playbook_levels(side, float(entry), float(atr))
    return f"{note}; code sl={stop} tp={target}"


def _verdict_word(verdict: str) -> str:
    match = _WORD.search(verdict or "")
    return match.group(0).upper() if match else ""


def verdict_decision(verdict: str, side: str) -> tuple[bool, str]:
    """Explicit approve or no entry. Empty and ambiguous verdicts fail closed."""
    word = _verdict_word(verdict)
    if not word:
        return False, "joe verdict missing"
    if _is_short(side):
        if word in _SHORT_APPROVE:
            return True, "joe approved short"
        return False, f"joe verdict {word} is not an explicit approve for a short"
    if word in _LONG_APPROVE:
        return True, "joe approved"
    return False, f"joe verdict {word} is not an explicit approve"


def consult_joe(question: str, url: str, timeout: float = CONSULT_TIMEOUT) -> dict:
    """POST {question} to Joe. Raises on transport, HTTP, and non-object replies.

    trust_env is off so a proxy cannot redirect a localhost consult.
    """
    if not url:
        raise RuntimeError("joe url missing")
    with httpx.Client(timeout=httpx.Timeout(timeout), trust_env=False) as client:
        response = client.post(url, json={"question": question})
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("joe reply is not a JSON object")
    return payload


def _source_count(payload: dict) -> str:
    sources = payload.get("sources")
    if isinstance(sources, list):
        return str(len(sources))
    return ""


def _sources_ok(payload: dict) -> tuple[bool, str]:
    sources = payload.get("sources") if isinstance(payload, dict) else None
    if sources is None or not isinstance(sources, list):
        return False, "joe sources missing"
    if len(sources) == 0:
        return False, "joe sources empty"
    return True, ""


def _log(
    signal,
    *,
    inventory: str,
    joe_ok: str,
    joe_verdict: str,
    joe_source_count: str,
    latency_s: str,
    action: str,
    reason: str,
    path: str,
) -> None:
    log_entry_decision(
        ticker=getattr(signal, "ticker", ""),
        pattern=signal_pattern(signal),
        inventory_status=inventory,
        joe_ok=joe_ok,
        joe_verdict=joe_verdict,
        joe_source_count=joe_source_count,
        latency_s=latency_s,
        action=action,
        reason=reason,
        path=path or None,
    )


def _settings_bits(settings: Settings | None) -> tuple[str, Path, str]:
    if settings is None:
        return DEFAULT_JOE_URL, Path(DEFAULT_PATTERN_INVENTORY), ""
    return settings.joe_url, Path(settings.pattern_inventory_path), settings.entry_decision_log


def joe_check(signal, *, settings: Settings | None = None) -> tuple[bool, str]:
    """Return (allow_entry, reason). allow_entry is True only when every check passes."""
    url, inventory_path, log_path = _settings_bits(settings)
    pattern = signal_pattern(signal)
    ticker = getattr(signal, "ticker", "") or ""
    status = inventory_status(inventory_path, pattern, ticker)
    if status != "active":
        if status in ("missing", "unparseable"):
            reason = f"inventory {status}"
        elif status == "absent":
            reason = f"inventory has no active row for {pattern_key(pattern)} / {ticker}"
        else:
            reason = f"inventory status {status}"
        _log(
            signal,
            inventory=status,
            joe_ok="",
            joe_verdict="",
            joe_source_count="",
            latency_s="",
            action="no-entry",
            reason=reason,
            path=log_path,
        )
        return False, reason

    levels = _levels_note(signal, "")
    question = (
        f"Paper entry review. Signal: {signal.side} {signal.ticker} on "
        f"'{pattern}' (hit {signal.hit:.0%}, n={signal.n}), "
        f"enter {signal.enter_on}, close {signal.close_on}. "
        "Answer with exactly one word: TAKE or SKIP."
    )
    started = time.perf_counter()
    try:
        payload = consult_joe(question, url, CONSULT_TIMEOUT)
    except httpx.TimeoutException:
        latency = f"{time.perf_counter() - started:.3f}"
        reason = f"joe consult timed out; {levels}"
        logger.warning("joe consult timed out for %s", getattr(signal, "id", ticker))
        _log(
            signal,
            inventory=status,
            joe_ok="false",
            joe_verdict="",
            joe_source_count="",
            latency_s=latency,
            action="no-entry",
            reason=reason,
            path=log_path,
        )
        return False, reason
    except Exception as exc:
        latency = f"{time.perf_counter() - started:.3f}"
        reason = f"joe consult failed: {type(exc).__name__}: {exc}; {levels}"
        logger.warning("joe consult failed for %s: %s", getattr(signal, "id", ticker), exc)
        _log(
            signal,
            inventory=status,
            joe_ok="false",
            joe_verdict="",
            joe_source_count="",
            latency_s=latency,
            action="no-entry",
            reason=reason,
            path=log_path,
        )
        return False, reason

    latency = f"{time.perf_counter() - started:.3f}"
    verdict = str(payload.get("verdict") or "")
    sources = _source_count(payload)
    levels = _levels_note(signal, verdict)
    if payload.get("ok") is not True:
        reason = f"joe ok is not true; {levels}"
        _log(
            signal,
            inventory=status,
            joe_ok="false",
            joe_verdict=verdict,
            joe_source_count=sources,
            latency_s=latency,
            action="no-entry",
            reason=reason,
            path=log_path,
        )
        return False, reason

    sources_ok, sources_reason = _sources_ok(payload)
    if not sources_ok:
        reason = f"{sources_reason}; {levels}"
        _log(
            signal,
            inventory=status,
            joe_ok="true",
            joe_verdict=verdict,
            joe_source_count=sources or "0",
            latency_s=latency,
            action="no-entry",
            reason=reason,
            path=log_path,
        )
        return False, reason

    allow, why = verdict_decision(verdict, (signal.side or "UP"))
    reason = f"{why}; {levels}"
    _log(
        signal,
        inventory=status,
        joe_ok="true",
        joe_verdict=verdict,
        joe_source_count=sources,
        latency_s=latency,
        action="enter" if allow else "no-entry",
        reason=reason,
        path=log_path,
    )
    return allow, reason
