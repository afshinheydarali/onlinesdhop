from __future__ import annotations

import re
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class Admin:
    telegram_id: int
    name: str
    admin_code: str
    is_active: bool


@dataclass(frozen=True, slots=True)
class SaveResult:
    order: dict[str, Any] | None
    created: bool = False
    duplicate_confirmation_required: bool = False


class Database:
    def __init__(self, path: str, duplicate_window_days: int = 30):
        self.path = path
        self.duplicate_window_days = duplicate_window_days

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS admins (
                    telegram_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    admin_code TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_id TEXT NOT NULL UNIQUE,
                    admin_telegram_id INTEGER NOT NULL REFERENCES admins(telegram_id),
                    customer_name TEXT NOT NULL,
                    phone_raw TEXT NOT NULL,
                    phone_normalized TEXT NOT NULL,
                    province TEXT NOT NULL,
                    city TEXT NOT NULL,
                    address TEXT NOT NULL,
                    postal_code TEXT,
                    product_raw TEXT NOT NULL,
                    product_normalized TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    amount INTEGER,
                    notes TEXT,
                    photo_file_id TEXT NOT NULL,
                    duplicate_of INTEGER REFERENCES orders(id),
                    draft_token TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    delivery_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (delivery_status IN ('pending', 'sending', 'sent', 'failed')),
                    delivery_attempts INTEGER NOT NULL DEFAULT 0,
                    delivery_error TEXT,
                    channel_photo_message_id INTEGER,
                    channel_text_message_id INTEGER,
                    delivered_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_orders_duplicate
                    ON orders(phone_normalized, product_normalized, created_at DESC);
                """
            )

    def add_admin(self, telegram_id: int, name: str, admin_code: str) -> Admin:
        if telegram_id <= 0:
            raise ValueError("Telegram User ID باید مثبت باشد.")
        name, admin_code = " ".join(name.strip().split()), admin_code.strip().upper()
        if not name or len(name) > 120 or not re.fullmatch(r"[A-Z0-9_-]{2,32}", admin_code):
            raise ValueError("نام یا کد ادمین نامعتبر است.")
        try:
            with closing(self._connect()) as connection:
                connection.execute(
                    "INSERT INTO admins (telegram_id, name, admin_code, created_at) VALUES (?, ?, ?, ?)",
                    (telegram_id, name, admin_code, datetime.now(UTC).isoformat()),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Telegram User ID یا کد ادمین قبلاً ثبت شده است.") from exc
        return Admin(telegram_id, name, admin_code, True)

    def get_admin(self, telegram_id: int, *, active_only: bool = True) -> Admin | None:
        sql = "SELECT telegram_id, name, admin_code, is_active FROM admins WHERE telegram_id = ?"
        params: tuple[object, ...] = (telegram_id,)
        if active_only:
            sql += " AND is_active = 1"
        with closing(self._connect()) as connection:
            row = connection.execute(sql, params).fetchone()
        return Admin(row["telegram_id"], row["name"], row["admin_code"], bool(row["is_active"])) if row else None

    def list_admins(self) -> list[Admin]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT telegram_id, name, admin_code, is_active FROM admins ORDER BY admin_code COLLATE NOCASE"
            ).fetchall()
        return [Admin(row["telegram_id"], row["name"], row["admin_code"], bool(row["is_active"])) for row in rows]

    def set_admin_active(self, telegram_id: int, active: bool) -> bool:
        with closing(self._connect()) as connection:
            cursor = connection.execute("UPDATE admins SET is_active = ? WHERE telegram_id = ?", (int(active), telegram_id))
        return cursor.rowcount == 1

    def find_duplicate(self, phone_normalized: str, product_normalized: str) -> dict[str, Any] | None:
        cutoff = (datetime.now(UTC) - timedelta(days=self.duplicate_window_days)).isoformat()
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT * FROM orders
                   WHERE phone_normalized = ? AND product_normalized = ? AND created_at >= ?
                   ORDER BY created_at DESC LIMIT 1""",
                (phone_normalized, product_normalized, cutoff),
            ).fetchone()
        return dict(row) if row else None

    def save_order(self, admin_id: int, draft: Mapping[str, Any], *, allow_duplicate: bool) -> SaveResult:
        now = datetime.now(UTC)
        cutoff = (now - timedelta(days=self.duplicate_window_days)).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            active_admin = connection.execute(
                "SELECT 1 FROM admins WHERE telegram_id = ? AND is_active = 1", (admin_id,)
            ).fetchone()
            if not active_admin:
                connection.rollback()
                raise PermissionError("Admin is not active")
            existing = connection.execute("SELECT * FROM orders WHERE draft_token = ?", (draft["draft_token"],)).fetchone()
            if existing:
                connection.commit()
                return SaveResult(dict(existing))
            duplicate = connection.execute(
                """SELECT id FROM orders
                   WHERE phone_normalized = ? AND product_normalized = ? AND created_at >= ?
                   ORDER BY created_at DESC LIMIT 1""",
                (draft["phone_normalized"], draft["product_normalized"], cutoff),
            ).fetchone()
            if duplicate and not allow_duplicate:
                connection.commit()
                return SaveResult(None, duplicate_confirmation_required=True)
            public_id = f"ORD-{now:%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
            cursor = connection.execute(
                """INSERT INTO orders (
                    public_id, admin_telegram_id, customer_name, phone_raw, phone_normalized,
                    province, city, address, postal_code, product_raw, product_normalized,
                    quantity, amount, notes, photo_file_id, duplicate_of, draft_token, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    public_id, admin_id, draft["customer_name"], draft["phone"], draft["phone_normalized"],
                    draft["province"], draft["city"], draft["address"], draft.get("postal_code"),
                    draft["product"], draft["product_normalized"], draft["quantity"], draft.get("amount"),
                    draft.get("notes"), draft["photo_file_id"], duplicate["id"] if duplicate else None,
                    draft["draft_token"], now.isoformat(),
                ),
            )
            row = connection.execute("SELECT * FROM orders WHERE id = ?", (cursor.lastrowid,)).fetchone()
            connection.commit()
        return SaveResult(dict(row), created=True)

    def get_order(self, public_id: str, *, admin_id: int | None = None) -> dict[str, Any] | None:
        sql: str = "SELECT * FROM orders WHERE public_id = ?"
        params: list[object] = [public_id]
        if admin_id is not None:
            sql += " AND admin_telegram_id = ?"
            params.append(admin_id)
        with closing(self._connect()) as connection:
            row = connection.execute(sql, params).fetchone()
        return dict(row) if row else None

    def get_order_by_id(self, order_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row) if row else None

    def claim_delivery(self, order_id: int) -> bool:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """UPDATE orders SET delivery_status = 'sending', delivery_attempts = delivery_attempts + 1,
                   delivery_error = NULL WHERE id = ? AND delivery_status IN ('pending', 'failed')""",
                (order_id,),
            )
        return cursor.rowcount == 1

    def mark_delivered(self, order_id: int, photo_message_id: int, text_message_id: int | None = None) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE orders SET delivery_status = 'sent', channel_photo_message_id = ?,
                   channel_text_message_id = ?, delivered_at = ?, delivery_error = NULL WHERE id = ?""",
                (photo_message_id, text_message_id, datetime.now(UTC).isoformat(), order_id),
            )

    def mark_photo_sent(self, order_id: int, photo_message_id: int) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE orders SET channel_photo_message_id = ? WHERE id = ? AND delivery_status = 'sending'",
                (photo_message_id, order_id),
            )

    def mark_delivery_failed(self, order_id: int, error: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE orders SET delivery_status = 'failed', delivery_error = ? WHERE id = ?",
                (error[:500], order_id),
            )

    def recover_interrupted_deliveries(self) -> int:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """UPDATE orders SET delivery_status = 'failed',
                   delivery_error = 'Process interrupted during delivery' WHERE delivery_status = 'sending'"""
            )
        return cursor.rowcount
