"""Keep every test off the repo logs directory.

Sell rows and entry rows both go through src.decision_log. Point those paths
at tmp_path, then fail the suite if <repo>/logs/ changed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import src.decision_log as decision_log
from src.config import REPO_ROOT

_REPO_LOGS = REPO_ROOT / "logs"


def _snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    found: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            found[str(path.relative_to(root))] = path.read_bytes()
    return found


@pytest.fixture(scope="session", autouse=True)
def _repo_logs_unchanged():
    before = _snapshot(_REPO_LOGS)
    yield
    after = _snapshot(_REPO_LOGS)
    assert after == before, "<repo>/logs/ changed during the suite"


@pytest.fixture(autouse=True)
def _decision_logs_in_tmp(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(decision_log, "LOG_DIR", log_dir)
    monkeypatch.setattr(decision_log, "LOG_PATH", log_dir / "decisions.csv")
    monkeypatch.setattr(decision_log, "ENTRY_LOG_PATH", log_dir / "entry_decisions.csv")
