from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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
    def from_env(cls) -> "Config":
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

