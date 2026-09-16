# Pattern Fleet research feedback connector

Brancher reads the optional local file
`data/portfolio/research_feedback/feedback.jsonl` for **read-only status and
reporting**. It is never consulted by signal merging, eligibility, risk,
shorting, sizing, or order execution.

Each line is one JSON object, keyed by the pair `ticker` + `pattern`:

```json
{"ticker":"AAPL","pattern":"earnings_gap","status":"active","evidence_refs":["vault://Patterns/AAPL/earnings_gap"],"recent_results":{"n":12,"hit":0.83},"oos_results":{"n":8,"hit":0.75},"regime_results":{"bull":0.9,"bear":0.5},"managed_results":{"n":4,"pnl":0.12},"last_seen":"2026-09-15T12:00:00Z","lesson":"Works best when the gap holds the opening range."}
```

Required fields are `ticker`, `pattern`, `status`, `evidence_refs`, and
`last_seen`; `status` must be `active`, `decaying`, or `dead`. Results are
preserved as JSON values for research consumers. Records older than 30 days,
future-dated records, malformed JSON, invalid fields, and unreadable files are
ignored safely. For duplicate keys, the newest valid `last_seen` wins.

`python3 -m src.cli status` exposes the valid records under
`research_feedback`, including counts by status. EOD reporting includes the
valid record count. No feedback record can create, cancel, or modify an order.
