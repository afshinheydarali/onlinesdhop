from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def load_env_file(path: str = ".env") -> None:
    env_file = Path(path)
    if not env_file.is_file():
        return
    for number, raw_line in enumerate(env_file.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not key:
            raise ValueError(f"Invalid .env line {number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    owner_telegram_id: int
    orders_channel_id: int
    database_path: str = "data/orders.sqlite3"
    duplicate_window_days: int = 30
    app_timezone: str = "Asia/Tehran"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env_file: str | None = ".env") -> "Config":
        if env_file:
            load_env_file(env_file)
        missing = [name for name in ("BOT_TOKEN", "OWNER_TELEGRAM_ID", "ORDERS_CHANNEL_ID") if not os.getenv(name)]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
        try:
            owner_id = int(os.environ["OWNER_TELEGRAM_ID"])
            channel_id = int(os.environ["ORDERS_CHANNEL_ID"])
            days = int(os.getenv("DUPLICATE_WINDOW_DAYS", "30"))
        except ValueError as exc:
            raise ValueError("OWNER_TELEGRAM_ID, ORDERS_CHANNEL_ID and DUPLICATE_WINDOW_DAYS must be integers") from exc
        if owner_id <= 0 or channel_id >= 0 or days < 1:
            raise ValueError("OWNER_TELEGRAM_ID must be positive, ORDERS_CHANNEL_ID negative, and duplicate days positive")
        timezone = os.getenv("APP_TIMEZONE", "Asia/Tehran")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown APP_TIMEZONE: {timezone}") from exc
        return cls(
            bot_token=os.environ["BOT_TOKEN"],
            owner_telegram_id=owner_id,
            orders_channel_id=channel_id,
            database_path=os.getenv("DATABASE_PATH", "data/orders.sqlite3"),
            duplicate_window_days=days,
            app_timezone=timezone,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
