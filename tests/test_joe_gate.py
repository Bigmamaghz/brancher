"""Fail-closed Joe entry gate.

Each test points Joe at its own localhost URL (closed port, hanging port, or a
fake). None of them use :8080. Decision rows go to tmp_path.

The fake reply shape is the captured consult() object:
{ok, verdict, sources:[{source, text}], ts}.
"""
from __future__ import annotations

import csv
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import create_autospec
from urllib.parse import urlparse

from alpaca.trading.client import TradingClient

import src.executor as ex
from src.book import Book
from src.config import REPO_ROOT, Settings
from src.joe_gate import joe_check
from src.merge import MergedSignal
from src.poll import Signal

TODAY = "2026-10-07"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pattern_inventory.md"
SELL_LOG = REPO_ROOT / "logs" / "decisions.csv"

# Captured consult() fields. Texts are short; the keys match the live reply.
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


def _joe_reply(verdict: str, sources: list[dict]) -> dict:
    return {
        "ok": True,
        "verdict": verdict,
        "sources": sources,
        "ts": 1791349741,
    }


def _require_local_url(url: str) -> None:
    parsed = urlparse(url)
    assert parsed.hostname in {"127.0.0.1", "localhost"}
    assert parsed.port is not None
    assert parsed.port != 8080


def _closed_url() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    url = f"http://127.0.0.1:{port}/consult"
    _require_local_url(url)
    return url


class _FakeJoe:
    """Localhost HTTP server that returns one JSON consult reply."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0
        self.url = ""
        self._httpd: ThreadingHTTPServer | None = None

    def __enter__(self) -> "_FakeJoe":
        owner = self
        body = json.dumps(self.payload).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.calls += 1
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length:
                    self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = self._httpd.server_address[1]
        self.url = f"http://127.0.0.1:{port}/consult"
        _require_local_url(self.url)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()


class _HangingJoe:
    """Accepts the TCP connection and never writes a response."""

    def __init__(self):
        self.calls = 0
        self.url = ""
        self._stop = threading.Event()
        self._sock: socket.socket | None = None

    def __enter__(self) -> "_HangingJoe":
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(5)
        port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/consult"
        _require_local_url(self.url)
        self._sock = sock
        threading.Thread(target=self._serve, daemon=True).start()
        return self

    def _serve(self) -> None:
        assert self._sock is not None
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.calls += 1
            conn.settimeout(0.2)
            while not self._stop.is_set():
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
            try:
                conn.close()
            except OSError:
                pass

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


class _Telegram:
    def send(self, text, dry_run=False):
        return True


def _settings(
    tmp_path: Path,
    joe_url: str,
    inventory: Path,
    max_opens_per_day: int = 5,
) -> Settings:
    _require_local_url(joe_url)
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
        joe_veto=True,
        joe_url=joe_url,
        pattern_inventory_path=str(inventory),
        entry_decision_log=str(tmp_path / "decisions.csv"),
    )


def _signal(ticker: str, pattern: str) -> Signal:
    return Signal(
        id=f"fleet-{ticker}-{pattern}",
        ticker=ticker,
        side="UP",
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


def test_closed_port_no_entry(tmp_path):
    before = _sell_log_bytes()
    settings = _settings(tmp_path, _closed_url(), FIXTURE)
    book, client = _drive(settings, _signal("FFIV", "20_TweezerBottom-2026-09-28"))
    _assert_no_order(book, client)
    rows = _rows(settings, tmp_path)
    assert rows[0]["action"] == "no-entry"
    assert rows[0]["ticker"] == "FFIV"
    assert "joe consult failed" in rows[0]["reason"]
    assert _sell_log_bytes() == before


def test_hanging_port_no_entry_within_15s(tmp_path):
    before = _sell_log_bytes()
    with _HangingJoe() as joe:
        settings = _settings(tmp_path, joe.url, FIXTURE)
        started = time.monotonic()
        book, client = _drive(settings, _signal("FFIV", "20_TweezerBottom-2026-09-28"))
        elapsed = time.monotonic() - started
    _assert_no_order(book, client)
    assert elapsed < 15
    assert joe.calls >= 1
    rows = _rows(settings, tmp_path)
    assert rows[0]["action"] == "no-entry"
    assert "timed out" in rows[0]["reason"]
    assert _sell_log_bytes() == before


def test_empty_sources_no_entry(tmp_path):
    before = _sell_log_bytes()
    reply = _joe_reply("TAKE", [])
    with _FakeJoe(reply) as joe:
        settings = _settings(tmp_path, joe.url, FIXTURE)
        book, client = _drive(settings, _signal("FFIV", "20_TweezerBottom-2026-09-28"))
        assert joe.calls == 1
    _assert_no_order(book, client)
    rows = _rows(settings, tmp_path)
    assert rows[0]["action"] == "no-entry"
    assert rows[0]["joe_ok"] == "true"
    assert rows[0]["joe_verdict"] == "TAKE"
    assert rows[0]["joe_source_count"] == "0"
    assert "sources" in rows[0]["reason"]
    assert _sell_log_bytes() == before


def test_wst_conflicting_no_entry(tmp_path):
    before = _sell_log_bytes()
    with _FakeJoe(_joe_reply("TAKE", list(_CAPTURED_SOURCES))) as joe:
        settings = _settings(tmp_path, joe.url, FIXTURE)
        book, client = _drive(settings, _signal("WST", "streak_5down"))
        assert joe.calls == 0
    _assert_no_order(book, client)
    rows = _rows(settings, tmp_path)
    assert rows[0]["action"] == "no-entry"
    assert rows[0]["ticker"] == "WST"
    assert rows[0]["inventory_status"] == "conflicting"
    assert _sell_log_bytes() == before


def test_tweezer_bottom_on_aapl_no_entry(tmp_path):
    before = _sell_log_bytes()
    with _FakeJoe(_joe_reply("TAKE", list(_CAPTURED_SOURCES))) as joe:
        settings = _settings(tmp_path, joe.url, FIXTURE)
        book, client = _drive(settings, _signal("AAPL", "20_TweezerBottom"))
        assert joe.calls == 0
    _assert_no_order(book, client)
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
    with _FakeJoe(_joe_reply("TAKE", list(_CAPTURED_SOURCES))) as joe:
        settings = _settings(tmp_path, joe.url, missing)
        book, client = _drive(settings, _signal("FFIV", "20_TweezerBottom-2026-09-28"))
        assert joe.calls == 0
    _assert_no_order(book, client)
    rows = _rows(settings, tmp_path)
    assert rows[0]["action"] == "no-entry"
    assert rows[0]["inventory_status"] == "missing"
    assert _sell_log_bytes() == before


def test_max_opens_zero_never_calls_joe(tmp_path):
    before = _sell_log_bytes()
    with _FakeJoe(_joe_reply("TAKE", list(_CAPTURED_SOURCES))) as joe:
        settings = _settings(tmp_path, joe.url, FIXTURE, max_opens_per_day=0)
        book, client = _drive(settings, _signal("FFIV", "20_TweezerBottom-2026-09-28"))
        assert joe.calls == 0
    _assert_no_order(book, client)
    rows = _rows(settings, tmp_path)
    assert len(rows) == 1
    assert rows[0]["action"] == "no-entry"
    assert "MAX_OPENS_PER_DAY" in rows[0]["reason"]
    assert rows[0]["joe_ok"] == ""
    assert rows[0]["joe_verdict"] == ""
    assert _sell_log_bytes() == before


def test_happy_path_gate_passes_max_opens_still_blocks(tmp_path):
    before = _sell_log_bytes()
    with _FakeJoe(_joe_reply("TAKE", list(_CAPTURED_SOURCES))) as joe:
        settings = _settings(tmp_path, joe.url, FIXTURE, max_opens_per_day=0)
        signal = _signal("FFIV", "20_TweezerBottom-2026-09-28")
        allow, reason = joe_check(signal, settings=settings)
        assert allow is True
        assert "joe approved" in reason
        assert "playbook_levels" in reason
        assert joe.calls == 1
        book, client = _drive(settings, signal)
        _assert_no_order(book, client)
        assert joe.calls == 1
    rows = _rows(settings, tmp_path)
    assert rows[0]["action"] == "enter"
    assert rows[0]["ticker"] == "FFIV"
    assert rows[0]["pattern"] == "20_TweezerBottom-2026-09-28"
    assert rows[0]["inventory_status"] == "active"
    assert rows[0]["joe_ok"] == "true"
    assert rows[0]["joe_verdict"] == "TAKE"
    assert int(rows[0]["joe_source_count"]) >= 1
    assert rows[1]["action"] == "no-entry"
    assert "MAX_OPENS_PER_DAY" in rows[1]["reason"]
    assert rows[1]["joe_source_count"] == ""
    assert _sell_log_bytes() == before
