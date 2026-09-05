"""Import a legacy SQLite database into PostgreSQL without changing its source."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.models import Admin, Order, Outbox, User

DISABLED_PASSWORD_HASH = "!legacy-import-disabled"
ADMIN_FIELDS = ("telegram_id", "name", "admin_code", "is_active", "created_at")
ORDER_FIELDS = ("id", "public_id", "admin_telegram_id", "customer_name", "phone_raw", "phone_normalized", "province", "city", "address", "postal_code", "product_raw", "product_normalized", "quantity", "amount", "notes", "photo_file_id", "duplicate_of", "draft_token", "created_at", "delivery_status", "delivery_attempts", "delivery_error", "channel_photo_message_id", "channel_text_message_id", "delivered_at")

def checksum(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def parse_utc(value: str | None, field: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}") from exc
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)

def read_source(source: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    path = Path(source)
    if not path.is_file():
        raise ValueError("source SQLite file does not exist")
    if path.with_name(path.name + "-wal").exists():
        raise ValueError("source has a live WAL; provide a consistent SQLite backup snapshot")
    before = checksum(source)
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"admins", "orders"} <= tables:
            raise ValueError("source must contain admins and orders tables")
        admins = [dict(row) for row in connection.execute("SELECT * FROM admins ORDER BY telegram_id")]
        orders = [dict(row) for row in connection.execute("SELECT * FROM orders ORDER BY id")]
    finally:
        connection.close()
    if checksum(source) != before:
        raise RuntimeError("source changed while being read")
    return admins, orders, before

def validate_source(admins: list[dict[str, Any]], orders: list[dict[str, Any]]) -> None:
    admin_ids = {row["telegram_id"] for row in admins}
    order_ids = {row["id"] for row in orders}
    if len(admin_ids) != len(admins) or len(order_ids) != len(orders):
        raise ValueError("source contains duplicate primary keys")
    for row in admins:
        if row["telegram_id"] <= 0 or not row["name"] or not row["admin_code"]:
            raise ValueError(f"invalid admin {row['telegram_id']}")
        parse_utc(row["created_at"], "admin.created_at")
    for row in orders:
        if row["id"] <= 0 or row["quantity"] <= 0 or not row["public_id"] or not row["draft_token"]:
            raise ValueError(f"invalid order {row['id']}")
        if row["admin_telegram_id"] not in admin_ids:
            raise ValueError(f"order {row['id']} references unknown admin")
        if row["duplicate_of"] is not None and row["duplicate_of"] not in order_ids:
            raise ValueError(f"order {row['id']} references unknown duplicate")
        if row["delivery_status"] not in {"pending", "sending", "sent", "failed"}:
            raise ValueError(f"invalid delivery status for order {row['id']}")
        parse_utc(row["created_at"], "order.created_at")
        parse_utc(row["delivered_at"], "order.delivered_at")

def immutable_admin(row: dict[str, Any]) -> dict[str, Any]:
    values = dict(row)
    values["is_active"] = bool(values["is_active"])
    values["created_at"] = parse_utc(values["created_at"], "admin.created_at")
    return {field: values[field] for field in ADMIN_FIELDS}

def immutable_order(row: dict[str, Any]) -> dict[str, Any]:
    values = {field: row[field] for field in ORDER_FIELDS}
    values["created_at"] = parse_utc(values["created_at"], "order.created_at")
    values["delivered_at"] = parse_utc(values["delivered_at"], "order.delivered_at")
    return values

async def reset_sequences(connection: Any) -> None:
    for table in ("users", "orders"):
        sequence = (await connection.execute(text("SELECT pg_get_serial_sequence(:table, 'id')"), {"table": table})).scalar_one()
        if sequence:
            await connection.execute(text(f"SELECT setval(:sequence, COALESCE((SELECT max(id) FROM {table}), 1), true)"), {"sequence": sequence})

async def run(source: str, destination: str, dry_run: bool, reset_seq: bool = True) -> int:
    admins, orders, source_hash = read_source(source)
    validate_source(admins, orders)
    engine = create_async_engine(destination)
    imported = 0
    try:
        async with engine.begin() as connection:
            if dry_run:
                print(f"source_sha256={source_hash} admins={len(admins)} orders={len(orders)} dry_run=true")
                return 0
            admin_users: dict[int, int] = {}
            for row in admins:
                expected = immutable_admin(row)
                found = (await connection.execute(text("SELECT telegram_id, name, admin_code, is_active, created_at FROM admins WHERE telegram_id=:id"), {"id": row["telegram_id"]})).mappings().first()
                if found and any(found[field] != expected[field] for field in ADMIN_FIELDS):
                    raise ValueError(f"admin conflict {row['telegram_id']}")
                if not found:
                    await connection.execute(Admin.__table__.insert().values(**expected))
                user = (await connection.execute(text("SELECT id, is_active, role FROM users WHERE telegram_id=:id"), {"id": row["telegram_id"]})).mappings().first()
                if user and (bool(user["is_active"]) != bool(row["is_active"]) or user["role"] != "manager"):
                    raise ValueError(f"user mapping conflict {row['telegram_id']}")
                if not user:
                    next_id = (await connection.execute(text("SELECT COALESCE(max(id), 0) + 1 FROM users"))).scalar_one()
                    user = (await connection.execute(User.__table__.insert().values(id=next_id, telegram_id=row["telegram_id"], username=f"legacy_{row['telegram_id']}", password_hash=DISABLED_PASSWORD_HASH, role="manager", is_active=bool(row["is_active"]), token_version=0).returning(User.id))).mappings().first()
                admin_users[row["telegram_id"]] = int(user["id"])
            for row in orders:
                expected = immutable_order(row)
                found = (await connection.execute(text("SELECT " + ",".join(ORDER_FIELDS) + " FROM orders WHERE id=:id"), {"id": row["id"]})).mappings().first()
                if found:
                    if any(found[field] != expected[field] for field in ORDER_FIELDS):
                        raise ValueError(f"order conflict {row['id']}")
                    continue
                values = dict(expected)
                values["created_by_id"] = admin_users[row["admin_telegram_id"]]
                await connection.execute(Order.__table__.insert().values(**values))
                status = row["delivery_status"]
                if status in {"pending", "sending", "failed"}:
                    await connection.execute(Outbox.__table__.insert().values(order_id=row["id"], status="ambiguous" if status == "sending" else status, attempts=row["delivery_attempts"], error_code=row["delivery_error"]))
                imported += 1
            if reset_seq:
                await reset_sequences(connection)
    finally:
        await engine.dispose()
    if checksum(source) != source_hash:
        raise RuntimeError("source changed during import")
    print(f"imported_orders={imported} source_sha256={source_hash}")
    return imported

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.source, args.destination, args.dry_run))

if __name__ == "__main__":
    main()
