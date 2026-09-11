from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def secret(name: str) -> str:
    path = os.getenv(name + "_FILE")
    value = Path(path).read_text().strip() if path else os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Set {name} or {name}_FILE")
    return value


def integer(name: str, default: int, low: int, high: int) -> int:
    n = int(os.getenv(name, str(default)))
    if not low <= n <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return n


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    database_url: str = field(repr=False)
    encryption_keys: str = field(repr=False)
    telegram_api: str = "https://api.telegram.org"
    workers: int = 4
    max_accounts: int = 100
    auth_ttl: int = 600
    max_file_bytes: int = 20 * 1024 * 1024
    history_pages: int = 20
    history_interval: int = 120
    retention_days: int = 30
    rich_ui: bool = True
    allowed_users: frozenset[int] = frozenset()

    @classmethod
    def load(cls) -> Settings:
        return cls(
            bot_token=secret("TELEGRAM_BOT_TOKEN"),
            database_url=secret("DATABASE_URL"),
            encryption_keys=secret("ENCRYPTION_KEYS"),
            telegram_api=os.getenv("TELEGRAM_API_URL", "https://api.telegram.org").rstrip("/"),
            workers=integer("WORKERS", 4, 1, 32),
            max_accounts=integer("MAX_ACCOUNTS", 100, 1, 10000),
            auth_ttl=integer("AUTH_TTL_SECONDS", 600, 60, 1800),
            max_file_bytes=integer("MAX_FILE_MB", 20, 1, 20) * 1024 * 1024,
            history_pages=integer("HISTORY_MAX_PAGES", 20, 1, 1000),
            history_interval=integer("HISTORY_INTERVAL_SECONDS", 120, 30, 3600),
            retention_days=integer("RETENTION_DAYS", 30, 1, 3650),
            rich_ui=os.getenv("RICH_UI", "true").lower() == "true",
            allowed_users=frozenset(int(s) for s in os.getenv("ALLOWED_TELEGRAM_IDS", "").split(",") if s.strip()),
        )
