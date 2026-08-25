from src.discover import format_discovery_report
from src.registry import BotConfig
from src.discover import PortProbe


def test_format_report_includes_miss():
    bots = [
        BotConfig(
            id="financials-xlf",
            name="Financials · XLF",
            base_url="http://127.0.0.1:8780",
            signals_path="/signals",
            api_key_env="FINANCIALS_XLF_API_KEY",
            enabled=True,
        )
    ]
    probes = [
        PortProbe(
            port=8780,
            listening=False,
            http_code=None,
            bot_id=None,
            bot_name=None,
            signal_count=None,
            matched_config_id=None,
            error="nothing listening",
        )
    ]
    report = format_discovery_report(probes, bots)
    assert "MISS financials-xlf" in report
