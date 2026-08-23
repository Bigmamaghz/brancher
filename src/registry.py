from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.config import BOTS_YAML


@dataclass(frozen=True)
class BotConfig:
    id: str
    name: str
    base_url: str
    signals_path: str
    api_key_env: str
    enabled: bool

    @property
    def signals_url(self) -> str:
        return self.base_url.rstrip("/") + self.signals_path

    def api_key(self) -> str:
        return os.getenv(self.api_key_env, "")


def load_bots(path: Path | None = None) -> list[BotConfig]:
    yaml_path = path or BOTS_YAML
    if not yaml_path.exists():
        raise FileNotFoundError(f"Bots config not found: {yaml_path}")

    with open(yaml_path) as f:
        data = yaml.safe_load(f) or {}

    bots = []
    for entry in data.get("bots", []):
        bots.append(
            BotConfig(
                id=entry["id"],
                name=entry["name"],
                base_url=entry["base_url"],
                signals_path=entry.get("signals_path", "/signals"),
                api_key_env=entry["api_key_env"],
                enabled=entry.get("enabled", True),
            )
        )
    return bots


def enabled_bots(path: Path | None = None) -> list[BotConfig]:
    return [b for b in load_bots(path) if b.enabled]
