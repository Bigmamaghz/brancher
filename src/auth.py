from __future__ import annotations

import secrets
from secrets import compare_digest


def bot_id_to_prefix(bot_id: str) -> str:
    """Convert bot_id to API key prefix: financials-xlf → financials_xlf_"""
    return bot_id.replace("-", "_") + "_"


def generate_api_key(bot_id: str) -> str:
    return bot_id_to_prefix(bot_id) + secrets.token_hex(32)


def validate_api_key(provided: str, expected: str) -> bool:
    if not provided or not expected:
        return False
    return compare_digest(provided, expected)
