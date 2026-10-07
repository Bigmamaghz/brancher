"""joe_gate.py — fail-closed entry gate for Brancher paper orders.

Joe is research-only: he never places orders and never texts. The inventory
check always runs. JOE_VETO=0 skips only the consult.

Order for each candidate that passed the daily open cap:
  1. Pattern inventory, read from disk on every check (no cache).
  2. Stop and target come only from levels.playbook_levels. Joe's verdict is a
     bare word; digits in that word are ignored and the mismatch is logged.
  3. Joe consult, when JOE_VETO is on. consult() runs in a child process that
     is killed at 10s. Timeout, kill, import error, non-zero exit, and bad
     JSON are no entry.

consult() is joe_consult.consult from JOE_DIR (default /Users/mybot/joe), the
same import the 292b1a6 gate used. Call assumed from that code:
    consult(question, timeout=<seconds>) -> {ok, verdict, sources, ts}
The Mac paste shows timeout forwarded to urlopen (the module default is 60s).
The def line itself was not in the paste. This gate still passes timeout=10
and kills the child at 10s so a hang cannot outlive the cutoff.

Approve words:
  Brancher cannot open a short. risk.check_eligible rejects every side except
  UP before this gate, so the live rule is TAKE or BUY.
  If a short signal is handed in anyway, the previous router is kept for
  direction: TAKE still approves, SELL or SHORT approves a short, BUY does not.

Cited sources:
  The pasted consult() builds sources from retrieval (live replies have 6).
  joe_levels / joe_facts / evidence_answer short-circuit with a synthetic
  single-source dict, and the paste does not mark those dicts. A reply with
  fewer than 2 sources is treated as that short-circuit and blocked.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from src.config import DEFAULT_JOE_DIR, DEFAULT_PATTERN_INVENTORY, Settings
from src.decision_log import log_entry_decision
from src.levels import playbook_levels

logger = logging.getLogger(__name__)

CONSULT_TIMEOUT = 10.0
# Real retrieve() returns 6 chunks. One source is the synthetic short-circuit.
MIN_CITED_SOURCES = 2

_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")
_WORD = re.compile(r"[A-Za-z]+")
_SHORT_SIDES = frozenset({"short", "sell", "down"})
_LONG_APPROVE = frozenset({"TAKE", "BUY"})
_SHORT_APPROVE = frozenset({"TAKE", "SELL", "SHORT"})

# Runs in a fresh interpreter. Inserts JOE_DIR, calls consult, writes one JSON
# object to the path in argv. Non-zero exit means the parent must not enter.
_CONSULT_CHILD = """
import json
import sys
from pathlib import Path

joe_dir, question, timeout_s, out_path = sys.argv[1:5]
out = Path(out_path)

def finish(payload, code):
    out.write_text(json.dumps(payload))
    raise SystemExit(code)

module_path = Path(joe_dir) / "joe_consult.py"
if not module_path.is_file():
    finish({"ok": False, "error": "import error: joe_consult.py missing", "verdict": "", "sources": []}, 1)
sys.path.insert(0, joe_dir)
try:
    from joe_consult import consult
    result = consult(question, timeout=float(timeout_s))
except Exception as exc:
    finish({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "verdict": "", "sources": []}, 1)
if not isinstance(result, dict):
    finish({"ok": False, "error": "consult() did not return an object", "verdict": "", "sources": []}, 1)
try:
    encoded = json.dumps(result)
except (TypeError, ValueError) as exc:
    finish({"ok": False, "error": "bad JSON: %s" % exc, "verdict": "", "sources": []}, 1)
out.write_text(encoded)
raise SystemExit(0)
"""

# Detached startup probe. One log line, then exit. Alarm so it cannot linger.
_SELF_CHECK_CHILD = """
import logging
import signal
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("src.joe_gate")

def _alarm(signum, frame):
    log.warning("joe self-check failed: timed out")
    raise SystemExit(0)

signal.signal(signal.SIGALRM, _alarm)
signal.alarm(10)
joe_dir = sys.argv[1]
module_path = Path(joe_dir) / "joe_consult.py"
if not module_path.is_file():
    log.warning("joe self-check failed: import error: joe_consult.py missing")
    raise SystemExit(0)
sys.path.insert(0, joe_dir)
try:
    from joe_consult import consult
    result = consult("startup self-check", timeout=10)
except Exception as exc:
    log.warning("joe self-check failed: %s", exc)
    raise SystemExit(0)
if isinstance(result, dict) and result.get("ok") is True:
    log.info("joe self-check ok")
else:
    log.warning("joe self-check failed: %s", result)
"""


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


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the child and anything it started. SIGKILL cannot be caught."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        proc.wait(timeout=2)


def _read_payload(path: Path) -> tuple[dict | None, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, "joe consult bad JSON"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, "joe consult bad JSON"
    if not isinstance(payload, dict):
        return None, "joe consult bad JSON"
    return payload, ""


def consult_in_child(question: str, joe_dir: str, timeout: float = CONSULT_TIMEOUT) -> tuple[dict | None, str]:
    """Run joe_consult.consult in a child process. Kill it at `timeout` seconds.

    Returns (payload, "") on a zero exit with a JSON object, including ok false.
    Returns (None, reason) when the child is killed, missing, or unreadable.
    """
    if not joe_dir:
        return None, "joe consult dir missing"
    fd, raw_path = tempfile.mkstemp(prefix="joe-consult-", suffix=".json")
    os.close(fd)
    out_path = Path(raw_path)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-c", _CONSULT_CHILD, joe_dir, question, str(timeout), str(out_path)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            return None, "joe consult killed after 10s"
        payload, bad = _read_payload(out_path)
        if code != 0:
            detail = ""
            if payload and payload.get("error"):
                detail = ": " + str(payload["error"])
            return None, f"joe consult failed: exit {code}{detail}"
        if payload is None:
            return None, bad or "joe consult bad JSON"
        return payload, ""
    finally:
        if proc.poll() is None:
            _kill_process_group(proc)
        try:
            out_path.unlink()
        except OSError:
            pass


def start_joe_self_check(joe_dir: str) -> None:
    """Log whether consult() can be reached. Never waits. Never runs under pytest.

    The child calls consult("startup self-check", timeout=10) and writes one
    line. An alarm at 10s exits it if the call hangs. The parent returns
    immediately so loop startup is not blocked.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if not joe_dir:
        logger.warning("joe self-check failed: joe consult dir missing")
        return
    try:
        subprocess.Popen(
            [sys.executable, "-c", _SELF_CHECK_CHILD, joe_dir],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
    except Exception as exc:
        logger.warning("joe self-check failed: %s", exc)


def _source_count(payload: dict) -> str:
    sources = payload.get("sources")
    if isinstance(sources, list):
        return str(len(sources))
    return ""


def _sources_ok(payload: dict) -> tuple[bool, str]:
    sources = payload.get("sources")
    if not isinstance(sources, list) or len(sources) == 0:
        return False, "joe sources empty"
    if len(sources) < MIN_CITED_SOURCES:
        return False, (
            "joe synthetic single source"
            f" (need at least {MIN_CITED_SOURCES}; retrieval returns more)"
        )
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


def _settings_bits(settings: Settings | None) -> tuple[str, Path, str, bool]:
    if settings is None:
        return DEFAULT_JOE_DIR, Path(DEFAULT_PATTERN_INVENTORY), "", True
    return (
        settings.joe_consult_dir,
        Path(settings.pattern_inventory_path),
        settings.entry_decision_log,
        settings.joe_veto,
    )


def _inventory_reason(status: str, pattern: str, ticker: str) -> str:
    if status in ("missing", "unparseable"):
        return f"inventory {status}"
    if status == "absent":
        return f"inventory has no active row for {pattern_key(pattern)} / {ticker}"
    return f"inventory status {status}"


def joe_check(signal, *, settings: Settings | None = None) -> tuple[bool, str]:
    """Return (allow_entry, reason). allow_entry is True only when every check passes."""
    joe_dir, inventory_path, log_path, joe_veto = _settings_bits(settings)
    pattern = signal_pattern(signal)
    ticker = getattr(signal, "ticker", "") or ""
    status = inventory_status(inventory_path, pattern, ticker)
    if status != "active":
        reason = _inventory_reason(status, pattern, ticker)
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
    if not joe_veto:
        reason = f"joe consult skipped (JOE_VETO=0); {levels}"
        _log(
            signal,
            inventory=status,
            joe_ok="",
            joe_verdict="",
            joe_source_count="",
            latency_s="",
            action="enter",
            reason=reason,
            path=log_path,
        )
        return True, reason

    question = (
        f"Paper entry review. Signal: {signal.side} {signal.ticker} on "
        f"'{pattern}' (hit {signal.hit:.0%}, n={signal.n}), "
        f"enter {signal.enter_on}, close {signal.close_on}. "
        "Answer with exactly one word: TAKE or SKIP."
    )
    started = time.perf_counter()
    payload, failure = consult_in_child(question, joe_dir, CONSULT_TIMEOUT)
    latency = f"{time.perf_counter() - started:.3f}"
    if payload is None:
        reason = f"{failure}; {levels}"
        logger.warning("joe consult blocked %s: %s", getattr(signal, "id", ticker), failure)
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
