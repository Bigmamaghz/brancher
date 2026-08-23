from src.auth import bot_id_to_prefix, generate_api_key, validate_api_key


def test_bot_id_to_prefix():
    assert bot_id_to_prefix("financials-xlf") == "financials_xlf_"


def test_generate_api_key_format():
    key = generate_api_key("financials-xlf")
    assert key.startswith("financials_xlf_")
    assert len(key) == len("financials_xlf_") + 64


def test_validate_api_key_accepts_match():
    key = generate_api_key("test-bot")
    assert validate_api_key(key, key) is True


def test_validate_api_key_rejects_mismatch():
    key = generate_api_key("test-bot")
    assert validate_api_key(key + "x", key) is False
    assert validate_api_key("", key) is False
    assert validate_api_key(key, "") is False
