"""Fail-closed Joe entry gate.

Joe is a fake joe_consult.py in a temp directory. The child loads that file.
No test contacts :8080, :11434, or /Users/mybot/joe. Decision rows go to tmp_path.

Reply shape matches a captured consult(): {ok, verdict, sources:[{source, text}], ts}.
"""
from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path
from unittest.mock import create_autospec, patch

from alpaca.trading.client import TradingClient

import src.executor as ex
from src.book import Book
from src.config import REPO_ROOT, Settings
from src.joe_gate import joe_check, start_joe_self_check
from src.merge import MergedSignal
from src.poll import Signal

TODAY = "2026-10-07"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pattern_inventory.md"
SELL_LOG = REPO_ROOT / "logs" / "decisions.csv"

_FAKE_MODULE = '''\
import json
import os
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def consult(question, timeout=60):
    calls = _HERE / "calls.txt"
    previous = int(calls.read_text() or "0") if calls.exists() else 0
    calls.write_text(str(previous + 1))
    mode = (_HERE / "mode.txt").read_text().strip()
    if mode == "hang":
        (_HERE / "pid").write_text(str(os.getpid()))
        time.sleep(60)
    if mode == "raise":
        raise RuntimeError("consult raised")
    return json.loads((_HERE / "reply.json").read_text())
'''

_CAPTURED_SOURCES = [
    {
        "source": "brancher__src__executor.py",
        "text": "def _process_enters(\n        self,\n        winners: list[MergedSignal],\n",
    },
    {
        "source": "brancher__src__joe_gate.py",
        "text": "joe_gate.py — joe's verdict gate for Brancher entries (paper-only stack).\n",
    },
]


def _joe_reply(verdict: str, sources: list[dict], *, ok: bool = True) -> dict:
    return {
        "ok": ok,
        "verdict": verdict,
        "sources": sources,
        "ts": 1791349741,
    }


def _isolated(path: Path) -> None:
    text = str(path.resolve())
    assert "/Users/mybot/joe" not in text
    assert "8080" not in text
    assert "11434" not in text


def _install_fake(directory: Path, *, mode: str = "reply", reply: dict | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    _isolated(directory)
    (directory / "joe_consult.py").write_text(_FAKE_MODULE)
    (directory / "mode.txt").write_text(mode)
    (directory / "calls.txt").write_text("0")
    if reply is not None:
        (directory / "reply.json").write_text(json.dumps(reply))
    return directory


def _calls(directory: Path) -> int:
    path = directory / "calls.txt"
    if not path.exists():
        return 0
    return int(path.read_text() or "0")


class _Telegram:
    def send(self, text, dry_run=False):
        return True


def _settings(
    tmp_path: Path,
    joe_dir: Path,
    inventory: Path,
    *,
    max_opens_per_day: int = 5,
    joe_veto: bool = True,
) -> Settings:
    _isolated(joe_dir)
    return Settings(
        alpaca_api_key="",
        alpaca_secret_key="",
        alpaca_base_url=None,
        telegram_bot_token="",
        telegram_chat_id="",
        paper_qty=1,
        paper_qty_max_mult=4,
        max_open_positions=10,
        max_opens_per_day=max_opens_per_day,
        min_hit=0.75,
        min_n=30,
        telegram_news_only=True,
        telegram_eod=False,
        telegram_trades_only=False,
        joe_veto=joe_veto,
        joe_consult_dir=str(joe_dir),
        pattern_inventory_path=str(inventory),
        entry_decision_log=str(tmp_path / "decisions.csv"),
    )


def _signal(ticker: str, pattern: str, *, side: str = "UP") -> Signal:
    return Signal(
        id=f"fleet-{ticker}-{pattern}",
        ticker=ticker,
        side=side,
        hit=0.80,
        n=40,
        event_type=pattern,
        enter_on=TODAY,
        close_on="2026-10-10",
        eligible=True,
        urgency="normal",
        sent_at="2026-10-07T00:00:00+00:00",
        net_r_full=0.2,
        net_r_recent=0.2,
        pattern_id=pattern,
    )


def _merged(signal: Signal) -> MergedSignal:
    return MergedSignal(signal=signal, bot_id="fleet-wave1", bot_name="Fleet")


def _drive(settings: Settings, signal: Signal):
    executor = ex.Executor(settings, dry_run=True)
    client = create_autospec(TradingClient, instance=True)
    executor._client = client
    executor.telegram = _Telegram()
    book = Book()
    executor._process_enters([_merged(signal)], book, TODAY)
    return book, client


def _assert_no_order(book: Book, client) -> None:
    assert book.positions == []
    assert client.submit_order.call_count == 0


def _rows(settings: Settings, tmp_path: Path) -> list[dict[str, str]]:
    path = Path(settings.entry_decision_log)
    assert path.resolve().is_relative_to(tmp_path.resolve())
    assert path.resolve() != SELL_LOG.resolve()
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def _sell_log_bytes() -> bytes | None:
    if SELL_LOG.exists():
        return SELL_LOG.read_bytes()
    return None


def test_consult_import_error_no_entry(tmp_path):
    before = _sell_log_bytes()
    joe_dir = tmp_path / "empty-joe"
    joe_dir.mkdir()
    settings = _settings(tmp_path, joe_dir, FIXTURE)
    book, client = _drive(settings, _signal("FFIV", "20_TweezerBottom-2026-09-28"))
    _assert_no_order(book, client)
    rows = _rows(settings, tmp_path)
    assert rows[0]["action"] == "no-entry"
    assert "import error" in rows[0]["reason"]
    assert _sell_log_bytes() == before


def test_consult_child_hang_is_killed_within_11s(tmp_path):
    before = _sell_log_bytes()
    joe_dir = _install_fake(tmp_path / "hang-joe", mode="hang")
    settings = _settings(tmp_path, joe_dir, FIXTURE)
    started = time.monotonic()
    allow, reason = joe_check(
        _signal("FFIV", "20_TweezerBottom-2026-09-28"),
        settings=settings,
    )
    elapsed = time.monotonic() - started
    assert allow is False
    assert "killed" in reason
    assert elapsed < 11.5
    pid = int((joe_dir / "pid").read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        dead = True
    else:
        dead = False
    assert dead, "hung consult child is still running"
    rows = _rows(settings, tmp_path)
    assert rows[0]["action"] == "no-entry"
    assert _sell_log_bytes() == before


def test_empty_sources_no_entry(tmp_path):
    before = _sell_log_bytes()
    joe_dir = _install_fake(tmp_path / "joe", reply=_joe_reply("TAKE", []))
    settings = _settings(tmp_path, joe_dir, FIXTURE)
    book, client = _drive(settings, _signal("FFIV", "20_TweezerBottom-2026-09-28"))
    _assert_no_order(book, client)
    assert _calls(joe_dir) == 1
    rows = _rows(settings, tmp_path)
    assert rows[0]["action"] == "no-entry"
    assert rows[0]["joe_ok"] == "true"
    assert rows[0]["joe_verdict"] == "TAKE"
    assert rows[0]["joe_source_count"] == "0"
    assert "sources" in rows[0]["reason"]
    assert _sell_log_bytes() == before


def test_wst_conflicting_no_entry(tmp_path):
    before = _sell_log_bytes()
    joe_dir = _install_fake(
        tmp_path / "joe", reply=_joe_reply("TAKE", list(_CAPTURED_SOURCES))
    )
    settings = _settings(tmp_path, joe_dir, FIXTURE)
    book, client = _drive(settings, _signal("WST", "streak_5down"))
    _assert_no_order(book, client)
    assert _calls(joe_dir) == 0
    rows = _rows(settings, tmp_path)
    assert rows[0]["action"] == "no-entry"
    assert rows[0]["ticker"] == "WST"
    assert rows[0]["inventory_status"] == "conflicting"
    assert _sell_log_bytes() == before


def test_tweezer_bottom_on_aapl_no_entry(tmp_path):
    before = _sell_log_bytes()
    joe_dir = _install_fake(
        tmp_path / "joe", reply=_joe_reply("TAKE", list(_CAPTURED_SOURCES))
    )
    settings = _settings(tmp_path, joe_dir, FIXTURE)
    book, client = _drive(settings, _signal("AAPL", "20_TweezerBottom"))
    _assert_no_order(book, client)
    assert _calls(joe_dir) == 0
    rows = _rows(settings, tmp_path)
    assert rows[0]["action"] == "no-entry"
    assert rows[0]["ticker"] == "AAPL"
    assert rows[0]["inventory_status"] == "absent"
    assert "AAPL" in rows[0]["reason"]
    assert _sell_log_bytes() == before


def test_inventory_missing_no_entry(tmp_path):
    before = _sell_log_bytes()
    missing = tmp_path / "Pattern-Inventory.md"
    assert not missing.exists()
    joe_dir = _install_fake(
        tmp_path / "joe", reply=_joe_reply("TAKE", list(_CAPTURED_SOURCES))
    )
    settings = _settings(tmp_path, joe_dir, missing)
    book, client = _drive(settings, _signal("FFIV", "20_TweezerBottom-2026-09-28"))
    _assert_no_order(book, client)
    assert _calls(joe_dir) == 0
    rows = _rows(settings, tmp_path)
    assert rows[0]["action"] == "no-entry"
    assert rows[0]["inventory_status"] == "missing"
    assert _sell_log_bytes() == before


def test_max_opens_zero_never_calls_joe(tmp_path):
    before = _sell_log_bytes()
    joe_dir = _install_fake(
        tmp_path / "joe", reply=_joe_reply("TAKE", list(_CAPTURED_SOURCES))
    )
    settings = _settings(tmp_path, joe_dir, FIXTURE, max_opens_per_day=0)
    book, client = _drive(settings, _signal("FFIV", "20_TweezerBottom-2026-09-28"))
    _assert_no_order(book, client)
    assert _calls(joe_dir) == 0
    rows = _rows(settings, tmp_path)
    assert len(rows) == 1
    assert rows[0]["action"] == "no-entry"
    assert "MAX_OPENS_PER_DAY" in rows[0]["reason"]
    assert rows[0]["joe_ok"] == ""
    assert rows[0]["joe_verdict"] == ""
    assert _sell_log_bytes() == before


def test_happy_path_gate_passes_max_opens_still_blocks(tmp_path):
    before = _sell_log_bytes()
    joe_dir = _install_fake(
        tmp_path / "joe", reply=_joe_reply("TAKE", list(_CAPTURED_SOURCES))
    )
    settings = _settings(tmp_path, joe_dir, FIXTURE, max_opens_per_day=0)
    signal = _signal("FFIV", "20_TweezerBottom-2026-09-28")
    allow, reason = joe_check(signal, settings=settings)
    assert allow is True
    assert "joe approved" in reason
    assert "playbook_levels" in reason
    assert _calls(joe_dir) == 1
    book, client = _drive(settings, signal)
    _assert_no_order(book, client)
    assert _calls(joe_dir) == 1
    rows = _rows(settings, tmp_path)
    assert rows[0]["action"] == "enter"
    assert rows[0]["ticker"] == "FFIV"
    assert rows[0]["pattern"] == "20_TweezerBottom-2026-09-28"
    assert rows[0]["inventory_status"] == "active"
    assert rows[0]["joe_ok"] == "true"
    assert rows[0]["joe_verdict"] == "TAKE"
    assert int(rows[0]["joe_source_count"]) >= 2
    assert rows[1]["action"] == "no-entry"
    assert "MAX_OPENS_PER_DAY" in rows[1]["reason"]
    assert rows[1]["joe_source_count"] == ""
    assert _sell_log_bytes() == before


def test_joe_ok_false_no_entry(tmp_path):
    before = _sell_log_bytes()
    joe_dir = _install_fake(
        tmp_path / "joe",
        reply=_joe_reply("TAKE", list(_CAPTURED_SOURCES), ok=False),
    )
    settings = _settings(tmp_path, joe_dir, FIXTURE)
    book, client = _drive(settings, _signal("FFIV", "20_TweezerBottom-2026-09-28"))
    _assert_no_order(book, client)
    assert _calls(joe_dir) == 1
    rows = _rows(settings, tmp_path)
    assert rows[0]["action"] == "no-entry"
    assert rows[0]["joe_ok"] == "false"
    assert "ok is not true" in rows[0]["reason"]
    assert _sell_log_bytes() == before


def test_buy_on_short_no_entry(tmp_path):
    before = _sell_log_bytes()
    joe_dir = _install_fake(
        tmp_path / "joe", reply=_joe_reply("BUY", list(_CAPTURED_SOURCES))
    )
    settings = _settings(tmp_path, joe_dir, FIXTURE)
    allow, reason = joe_check(
        _signal("FFIV", "20_TweezerBottom", side="short"),
        settings=settings,
    )
    assert allow is False
    assert "short" in reason
    assert _calls(joe_dir) == 1
    rows = _rows(settings, tmp_path)
    assert rows[0]["action"] == "no-entry"
    assert rows[0]["joe_verdict"] == "BUY"
    assert _sell_log_bytes() == before


def test_synthetic_single_source_no_entry(tmp_path):
    before = _sell_log_bytes()
    joe_dir = _install_fake(
        tmp_path / "joe",
        reply=_joe_reply("TAKE", [_CAPTURED_SOURCES[0]]),
    )
    settings = _settings(tmp_path, joe_dir, FIXTURE)
    book, client = _drive(settings, _signal("FFIV", "20_TweezerBottom-2026-09-28"))
    _assert_no_order(book, client)
    assert _calls(joe_dir) == 1
    rows = _rows(settings, tmp_path)
    assert rows[0]["action"] == "no-entry"
    assert rows[0]["joe_source_count"] == "1"
    assert "synthetic" in rows[0]["reason"]
    assert _sell_log_bytes() == before


def test_joe_veto_off_inventory_miss_still_blocks(tmp_path):
    before = _sell_log_bytes()
    missing = tmp_path / "no-inventory.md"
    joe_dir = _install_fake(
        tmp_path / "joe", reply=_joe_reply("TAKE", list(_CAPTURED_SOURCES))
    )
    settings = _settings(tmp_path, joe_dir, missing, joe_veto=False)
    book, client = _drive(settings, _signal("FFIV", "20_TweezerBottom-2026-09-28"))
    _assert_no_order(book, client)
    assert _calls(joe_dir) == 0
    rows = _rows(settings, tmp_path)
    assert rows[0]["action"] == "no-entry"
    assert rows[0]["inventory_status"] == "missing"
    assert _sell_log_bytes() == before


def test_self_check_does_not_run_under_pytest():
    with patch("src.joe_gate.subprocess.Popen") as popen:
        start_joe_self_check("/tmp/not-the-real-joe")
    assert popen.call_count == 0
