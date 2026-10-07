"""Fix-2 gate test: pattern passes iff n >= MIN_N AND net avg R > 0 on BOTH
full history and trailing 365d (replaces the old win%-vs-XLV MIN_HIT gate).

Standalone:
  PYTHONPATH=. /Users/mybot/joe/trainvenv/bin/python tests/test_min_n_gate.py
"""
from __future__ import annotations

from src.config import Settings
from src.risk import check_eligible
from src.poll import Signal


def _settings(min_n: int = 30) -> Settings:
    return Settings(
        alpaca_api_key="", alpaca_secret_key="", alpaca_base_url=None,
        telegram_bot_token="", telegram_chat_id="",
        paper_qty=1, paper_qty_max_mult=1,
        max_open_positions=10, max_opens_per_day=5, min_hit=0.75, min_n=min_n,
        telegram_news_only=True, telegram_eod=False, telegram_trades_only=False,
        joe_veto=False,
    )


def _sig(n: int = 115, hit: float = 0.55, net_r_full: float = 0.2, net_r_recent: float = 0.2) -> Signal:
    return Signal(
        id="fleet-wave1-ZZZ-test", ticker="ZZZ", side="UP", hit=hit, n=n,
        event_type="test", enter_on="2026-09-29", close_on="2026-10-03",
        eligible=True, urgency="normal", sent_at="2026-09-29T00:00:00+00:00",
        net_r_full=net_r_full, net_r_recent=net_r_recent,
    )


def test_n_below_min_rejected():
    assert check_eligible(_sig(n=29, net_r_full=0.2, net_r_recent=0.2), _settings()) == "n 29 below MIN_N 30"
    assert check_eligible(_sig(n=8, net_r_full=0.1, net_r_recent=0.1), _settings()) == "n 8 below MIN_N 30"


def test_net_r_full_not_positive_rejected():
    # market_dip_gap_hold case: full negative despite n>=30 hypothetically
    assert "net-R not positive" in check_eligible(_sig(n=100, net_r_full=-0.045, net_r_recent=0.4), _settings())


def test_net_r_recent_not_positive_rejected():
    assert "net-R not positive" in check_eligible(_sig(n=100, net_r_full=0.2, net_r_recent=-0.01), _settings())


def test_net_r_unavailable_rejected():
    assert check_eligible(_sig(n=100, net_r_full=None, net_r_recent=None), _settings()) == "net-R unavailable"


def test_passes_when_n_and_both_windows_positive():
    # the three fear-family patterns: n>=30 and net R>0 both windows
    for nf, nr in ((0.193, 0.380), (0.108, 0.269), (0.157, 0.458)):
        assert check_eligible(_sig(n=200, net_r_full=nf, net_r_recent=nr), _settings()) is None


def test_hit_no_longer_gates():
    # old MIN_HIT would reject hit 0.557; now hit is ignored (net-R gates)
    assert check_eligible(_sig(n=115, hit=0.557, net_r_full=0.108, net_r_recent=0.269), _settings()) is None


def _run_standalone():
    import traceback
    tests = [("test_n_below_min_rejected", test_n_below_min_rejected),
             ("test_net_r_full_not_positive_rejected", test_net_r_full_not_positive_rejected),
             ("test_net_r_recent_not_positive_rejected", test_net_r_recent_not_positive_rejected),
             ("test_net_r_unavailable_rejected", test_net_r_unavailable_rejected),
             ("test_passes_when_n_and_both_windows_positive", test_passes_when_n_and_both_windows_positive),
             ("test_hit_no_longer_gates", test_hit_no_longer_gates)]
    results = []
    for name, fn in tests:
        try:
            fn(); results.append((name, "PASS", ""))
        except AssertionError as e:
            results.append((name, "FAIL", str(e)))
        except Exception as e:
            results.append((name, "ERROR", f"{type(e).__name__}: {e}\n" + traceback.format_exc()))
    print("=" * 60)
    print("fix-2 gate (MIN_N=30 + net R>0 both windows) — no orders")
    print("=" * 60)
    for name, status, detail in results:
        print(f"[{status}] {name}")
        if detail:
            for line in detail.rstrip().splitlines():
                print(f"        {line}")
    npass = sum(1 for _, s, _ in results if s == "PASS")
    print("-" * 60)
    print(f"{npass}/{len(results)} passed")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
