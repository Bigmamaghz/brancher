from __future__ import annotations

import json
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

from src.config import BOTS_YAML, REPO_ROOT
from src.registry import BotConfig, load_bots


@dataclass
class PortProbe:
    port: int
    listening: bool
    http_code: int | None
    bot_id: str | None
    bot_name: str | None
    signal_count: int | None
    matched_config_id: str | None
    error: str | None


def _listening_ports(start: int = 8780, end: int = 8790) -> set[int]:
    found: set[int] = set()
    try:
        out = subprocess.check_output(
            ["lsof", "-nP", f"-iTCP:{start}-{end}", "-sTCP:LISTEN"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            if ":" not in line:
                continue
            # ... TCP 127.0.0.1:8780 (LISTEN)
            for token in line.split():
                if ":" in token and token.rsplit(":", 1)[-1].isdigit():
                    port = int(token.rsplit(":", 1)[-1])
                    if start <= port <= end:
                        found.add(port)
    except (FileNotFoundError, subprocess.CalledProcessError):
        # Fall back: try connecting
        for port in range(start, end + 1):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    found.add(port)
            except OSError:
                pass
    return found


def _probe_signals(port: int, api_key: str = "") -> tuple[int | None, dict | None, str | None]:
    url = f"http://127.0.0.1:{port}/signals"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        req = Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=3) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return resp.status, None, "non-json body"
            return resp.status, data, None
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        data = None
        try:
            data = json.loads(body) if body else None
        except json.JSONDecodeError:
            pass
        return exc.code, data, None
    except URLError as exc:
        return None, None, str(exc.reason if hasattr(exc, "reason") else exc)
    except Exception as exc:
        return None, None, str(exc)


def discover(
    ports: range | None = None,
    bots: list[BotConfig] | None = None,
) -> list[PortProbe]:
    """Scan localhost ports and identify which Author is on each."""
    ports = ports or range(8780, 8791)
    bots = bots if bots is not None else load_bots()
    listening = _listening_ports(min(ports), max(ports))
    results: list[PortProbe] = []

    for port in ports:
        is_up = port in listening
        # Always try HTTP even if lsof missed it
        code, data, err = _probe_signals(port)
        if code is None and not is_up:
            results.append(
                PortProbe(
                    port=port,
                    listening=False,
                    http_code=None,
                    bot_id=None,
                    bot_name=None,
                    signal_count=None,
                    matched_config_id=None,
                    error=err or "nothing listening",
                )
            )
            continue

        bot_id = (data or {}).get("bot_id") if isinstance(data, dict) else None
        bot_name = (data or {}).get("bot_name") if isinstance(data, dict) else None
        signals = (data or {}).get("signals") if isinstance(data, dict) else None
        matched = None

        # If 401, try each bot key until one works
        if code == 401 or (code == 200 and not bot_id):
            for bot in bots:
                key = bot.api_key()
                if not key:
                    continue
                c2, d2, e2 = _probe_signals(port, key)
                if c2 == 200 and isinstance(d2, dict):
                    code, data, err = c2, d2, e2
                    bot_id = d2.get("bot_id") or bot.id
                    bot_name = d2.get("bot_name") or bot.name
                    signals = d2.get("signals")
                    matched = bot.id
                    break

        if not matched and bot_id:
            for bot in bots:
                if bot.id == bot_id:
                    matched = bot.id
                    break

        results.append(
            PortProbe(
                port=port,
                listening=True,
                http_code=code,
                bot_id=bot_id,
                bot_name=bot_name,
                signal_count=len(signals) if isinstance(signals, list) else None,
                matched_config_id=matched,
                error=err,
            )
        )
    return results


def apply_discovered_ports(
    probes: list[PortProbe],
    yaml_path: Path | None = None,
) -> list[str]:
    """Rewrite bots.yaml base_url from discovered live Authors. Returns change notes."""
    path = yaml_path or BOTS_YAML
    if not path.exists():
        example = REPO_ROOT / "config" / "bots.yaml.example"
        path.write_text(example.read_text() if example.exists() else "bots: []\n")

    data = yaml.safe_load(path.read_text()) or {"bots": []}
    bots = data.get("bots", [])
    changes: list[str] = []

    # Map bot_id -> live port
    live: dict[str, PortProbe] = {}
    for p in probes:
        if p.http_code == 200 and p.matched_config_id:
            live[p.matched_config_id] = p
        elif p.http_code == 200 and p.bot_id:
            live[p.bot_id] = p

    for entry in bots:
        bid = entry.get("id")
        if bid not in live:
            if entry.get("enabled", True):
                # leave enabled; doctor will still show fail
                pass
            continue
        probe = live[bid]
        new_url = f"http://127.0.0.1:{probe.port}"
        old_url = entry.get("base_url", "")
        if old_url != new_url:
            changes.append(f"{bid}: {old_url} → {new_url}")
            entry["base_url"] = new_url
        if not entry.get("enabled", True):
            entry["enabled"] = True
            changes.append(f"{bid}: enabled true")

    if changes:
        path.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
    return changes


def format_discovery_report(probes: list[PortProbe], bots: list[BotConfig]) -> str:
    lines = [
        "",
        "Brancher find — localhost Author discovery",
        "-" * 60,
    ]
    live = [p for p in probes if p.http_code in (200, 401) or p.listening]
    if not live:
        lines.append("  No Author servers found on 127.0.0.1:8780-8790")
    for p in probes:
        if not p.listening and p.http_code is None:
            continue
        who = p.bot_name or p.bot_id or p.matched_config_id or "?"
        sig = f"{p.signal_count} signals" if p.signal_count is not None else ""
        code = f"HTTP {p.http_code}" if p.http_code else (p.error or "")
        lines.append(f"  :{p.port}  {who:28} {code} {sig}".rstrip())

    lines.append("-" * 60)
    lines.append("Config vs reality:")
    by_port = {p.port: p for p in probes}
    for bot in bots:
        try:
            port = int(bot.base_url.rsplit(":", 1)[-1])
        except ValueError:
            port = -1
        probe = by_port.get(port)
        if probe and probe.http_code == 200:
            lines.append(f"  OK   {bot.id} on {bot.base_url}")
        elif probe and probe.listening:
            lines.append(f"  BAD  {bot.id} expected {bot.base_url} — port up but auth/signals failed ({probe.http_code or probe.error})")
        else:
            # Is this bot on another port?
            alt = next((p for p in probes if p.matched_config_id == bot.id or p.bot_id == bot.id), None)
            if alt and alt.http_code == 200:
                lines.append(f"  MOVE {bot.id} is on :{alt.port} (config says {bot.base_url})")
            else:
                lines.append(f"  MISS {bot.id} — not on this machine at {bot.base_url}")
    lines.append("-" * 60)
    return "\n".join(lines)
