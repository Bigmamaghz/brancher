"""joe_gate.py — joe's verdict gate for Brancher entries (paper-only stack).

joe is research-only: he never places orders and never texts. When JOE_VETO=1
(default), every candidate entry is consulted with joe before the buy. A
low-conviction verdict (WAIT/HOLD/AVOID, or a direction that disagrees with the
signal side) skips the entry — logged as a SKIP, never silent.

Hard rules:
  - Deterministic checks (bench, eligibility, stale window, caps, sizing) run
    FIRST. joe only vets signals that already passed every hard rule.
  - Sells/exit are NEVER gated: an exit vetoed by any model is a held loser.
  - Infrastructure failure (joe down, timeout) fails OPEN: the trade proceeds
    and the failure is logged. joe being offline must never stall the stack.
  - Verdicts are cached per signal id so repeated 15-min cycles don't re-brief
    joe on the same setup.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

from src.config import BOOK_PATH

logger = logging.getLogger(__name__)

JOE_DIR = Path("/Users/mybot/joe")
CACHE_PATH = BOOK_PATH.parent / "joe_gate_cache.jsonl"
CONSULT_TIMEOUT = 90  # seconds; joe is a 0.8B local model, normally <20s

# Words that mean "don't take this trade right now".
_WAIT_WORDS = ("WAIT", "HOLD", "AVOID", "SKIP", "PASS", "STAND ASIDE", "NO TRADE", "NO TRADE>")


def _consult(question: str) -> dict:
    """Import joe_consult from /Users/mybot/joe and ask. Returns its dict; never raises here."""
    if str(JOE_DIR) not in sys.path:
        sys.path.insert(0, str(JOE_DIR))
    from joe_consult import consult  # noqa: E402

    return consult(question, timeout=CONSULT_TIMEOUT)


def verdict_decision(verdict: str, side: str) -> tuple[bool, str]:
    """Map joe's verdict text to (allow_entry, reason) for a signal with `side`.

    Contract: the gate question asks joe to answer TAKE or SKIP (side-agnostic —
    the signal's side is already decided by the Author). Router is deterministic
    keyword matching, no LLM arithmetic. Legacy BUY/SELL verdicts (from joe's
    system-prompt habit) are still routed safely: BUY on a long = take, anything
    directional that disagrees with the signal side = veto.
    """
    v = (verdict or "").strip().upper()
    if not v:
        return True, "joe empty verdict -> fail-open"

    first = v.split(":", 1)[0]
    if first.startswith("TAKE") or "APPROVE" in first:
        return True, "joe approved"
    if first.startswith("SKIP") or first.startswith("VETO") or any(w in v for w in _WAIT_WORDS):
        return False, "joe says skip"

    bullish = first.startswith(("BUY", "LONG"))
    bearish = first.startswith(("SELL", "SHORT"))
    if bullish and not bearish:
        return (side != "short"), "joe bullish"
    if bearish and not bullish:
        return (side == "short"), "joe bearish"
    # Ambiguous verdict (no clear direction, no wait word) -> fail-open but note it.
    return True, "joe ambiguous -> fail-open"


def _cache_load() -> dict:
    try:
        if CACHE_PATH.exists():
            recs = [json.loads(l) for l in CACHE_PATH.read_text().splitlines() if l.strip()]
            # last verdict wins; key includes signal id only (enter_on is inside the id)
            return {r["signal_id"]: r for r in recs if r.get("signal_id")}
    except Exception:
        pass
    return {}


def _cache_put(rec: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CACHE_PATH.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        logger.warning("joe_gate cache write failed", exc_info=True)


def joe_check(signal, *, use_cache: bool = True) -> tuple[bool, str]:
    """Return (allow_entry, reason) for a candidate Signal.

    allow_entry=True  -> proceed (joe approved, ambiguous, or joe unavailable)
    allow_entry=False -> veto (SKIP logged by caller)
    """
    cached = _cache_load().get(signal.id) if use_cache else None
    if cached:
        return cached["allow"], "cache: " + cached["reason"]

    question = (
        f"Paper entry review. Signal: {signal.side} {signal.ticker} on "
        f"'{signal.event_type}' (hit {signal.hit:.0%}, n={signal.n}), "
        f"enter {signal.enter_on}, close {signal.close_on}. "
        f"Vault rules apply. Should Brancher take this paper entry? "
        "Answer with exactly one word first: TAKE or SKIP. Then one line of reason."
    )
    try:
        res = _consult(question)
    except Exception as exc:  # belt-and-braces: consult() already catches, this can't burn us
        logger.warning("joe consult crashed for %s: %s", signal.id, exc)
        return True, "joe consult crashed -> fail-open"

    if not res.get("ok"):
        logger.warning("joe unavailable for %s: %s", signal.id, res.get("error"))
        rec = {"signal_id": signal.id, "ts": int(time.time()), "allow": True,
               "reason": "joe unavailable -> fail-open", "verdict": ""}
        _cache_put(rec)
        return True, "joe unavailable -> fail-open"

    allow, reason = verdict_decision(res.get("verdict", ""), (signal.side or "long").lower())
    rec = {"signal_id": signal.id, "ts": int(time.time()), "allow": allow,
           "reason": reason, "verdict": res.get("verdict", "")[:300]}
    _cache_put(rec)
    return allow, reason
