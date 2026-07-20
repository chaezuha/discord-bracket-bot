"""Environment configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

# Above this the rendered image gets unwieldy and rounds stop being fun.
HARD_MAX_ITEMS = 64
DEFAULT_MAX_ITEMS = 32


class ConfigError(ValueError):
    """Raised when an environment variable is missing or malformed."""


@dataclass(frozen=True)
class Config:
    token: str
    dev_guild_id: int | None
    max_items: int
    db_path: str
    log_dir: str


def _parse_int(env: Mapping[str, str], key: str) -> int | None:
    raw = (env.get(key) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be an integer, got {raw!r}.") from None


def load_config(env: Mapping[str, str] = os.environ) -> Config:
    token = (env.get("DISCORD_TOKEN") or "").strip()
    if not token:
        raise ConfigError("DISCORD_TOKEN is not set. Copy .env.example to .env and add your token.")

    max_items = _parse_int(env, "MAX_ITEMS")
    if max_items is None:
        max_items = DEFAULT_MAX_ITEMS
    if not 2 <= max_items <= HARD_MAX_ITEMS:
        raise ConfigError(f"MAX_ITEMS must be between 2 and {HARD_MAX_ITEMS}, got {max_items}.")

    return Config(
        token=token,
        dev_guild_id=_parse_int(env, "DEV_GUILD_ID"),
        max_items=max_items,
        db_path=(env.get("DB_PATH") or "").strip() or os.path.join("data", "brackets.db"),
        log_dir=(env.get("LOG_DIR") or "").strip() or "logs",
    )
