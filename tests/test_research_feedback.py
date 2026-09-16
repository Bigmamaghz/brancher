import json
from datetime import datetime, timezone

from src.research_feedback import load_feedback


NOW = datetime(2026, 9, 16, 16, 0, tzinfo=timezone.utc)


def record(**overrides):
    value = {
        "ticker": "AAPL",
        "pattern": "earnings_gap",
        "status": "active",
        "evidence_refs": ["vault://Patterns/AAPL/earnings_gap"],
        "recent_results": {"n": 12, "hit": 0.83},
        "oos_results": {"n": 8, "hit": 0.75},
        "regime_results": {"bull": 0.9, "bear": 0.5},
        "managed_results": {"n": 4, "pnl": 0.12},
        "last_seen": "2026-09-15T12:00:00Z",
        "lesson": "Works best when the gap holds the opening range.",
    }
    value.update(overrides)
    return value


def test_reader_accepts_contract_and_deduplicates_newest(tmp_path):
    path = tmp_path / "feedback.jsonl"
    path.write_text("\n".join([
        json.dumps(record(status="decaying", last_seen="2026-09-14T12:00:00Z")),
        json.dumps(record(lesson="new lesson")),
    ]))

    records = load_feedback(path, now=NOW)

    assert len(records) == 1
    assert records[0].key == "AAPL:earnings_gap"
    assert records[0].lesson == "new lesson"


def test_reader_ignores_malformed_and_stale_records(tmp_path):
    path = tmp_path / "feedback.jsonl"
    path.write_text("\n".join([
        "not json",
        json.dumps(record(status="unknown")),
        json.dumps(record(last_seen="2026-01-01T00:00:00Z")),
        json.dumps(record(evidence_refs=["ok", 3])),
        json.dumps(record(ticker="MSFT")),
    ]))

    records = load_feedback(path, now=NOW)

    assert [item.ticker for item in records] == ["MSFT"]
