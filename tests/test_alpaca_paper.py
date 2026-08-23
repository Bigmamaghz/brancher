import pytest

from src.config import PaperOnlyError, _validate_paper_url


def test_paper_url_accepted():
    assert _validate_paper_url("https://paper-api.alpaca.markets") is not None


def test_live_url_refused():
    with pytest.raises(PaperOnlyError, match="paper-only"):
        _validate_paper_url("https://api.alpaca.markets")


def test_live_url_refused_partial():
    with pytest.raises(PaperOnlyError):
        _validate_paper_url("https://api.alpaca.markets/v2")
