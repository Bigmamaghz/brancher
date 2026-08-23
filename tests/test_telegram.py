from src.telegram import DISCLAIMER, format_message


def test_telegram_includes_source():
    msg = format_message(
        kind="ENTER",
        source="Financials · XLF",
        signal_line="CME material_agreement UP 88%",
        action="ENTER buy qty=3",
        detail="order=abc123",
        dates="ENTER: 2026-11-16 close · SELL: 2026-11-17 next session",
    )
    assert "SOURCE: Financials · XLF" in msg
    assert "KIND: ENTER" in msg
    assert DISCLAIMER in msg
    assert msg.startswith("[Brancher · Portfolio]")
