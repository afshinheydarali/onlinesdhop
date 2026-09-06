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
ORDER_FIELDS = (
    "id",
    "public_id",
    "admin_telegram_id",
    "customer_name",
    "phone_raw",
    "phone_normalized",
    "province",
    "city",
    "address",
    "postal_code",
    "product_raw",
    "product_normalized",
    "quantity",
    "amount",
    "notes",
    "photo_file_id",
    "duplicate_of",
    "draft_token",
    "created_at",
    "delivery_status",
    "delivery_attempts",
    "delivery_error",
    "channel_photo_message_id",
    "channel_text_message_id",
    "delivered_at",
)


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
        raise ValueError(
            "source has a live WAL; provide a consistent SQLite backup snapshot"
        )
    before = checksum(source)
    # Path.as_uri() correctly escapes Windows drive letters, spaces and '#'.
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"admins", "orders"} <= tables:
            raise ValueError("source must contain admins and orders tables")
        admins = [
            dict(row)
            for row in connection.execute("SELECT * FROM admins ORDER BY telegram_id")
        ]
        orders = [
            dict(row) for row in connection.execute("SELECT * FROM orders ORDER BY id")
        ]
    finally:
        connection.close()
    if checksum(source) != before:
        raise RuntimeError("source changed while being read")
    return admins, orders, before


def validate_source(admins: list[dict[str, Any]], orders: list[dict[str, Any]]) -> None:
    admin_ids = {row["telegram_id"] for row in admins}
    admin_codes = {str(row["admin_code"]).casefold() for row in admins}
    order_ids = {row["id"] for row in orders}
    public_ids = {row["public_id"] for row in orders}
    draft_tokens = {row["draft_token"] for row in orders}
    if (
        len(admin_ids) != len(admins)
        or len(admin_codes) != len(admins)
        or len(order_ids) != len(orders)
    ):
        raise ValueError("source contains duplicate primary keys")
    if len(public_ids) != len(orders) or len(draft_tokens) != len(orders):
        raise ValueError("source contains duplicate order identifiers")
    for row in admins:
        if (
            row["telegram_id"] <= 0
            or not row["name"]
            or not row["admin_code"]
            or len(row["name"]) > 120
            or len(row["admin_code"]) > 32
        ):
            raise ValueError(f"invalid admin {row['telegram_id']}")
        parse_utc(row["created_at"], "admin.created_at")
    for row in orders:
        if (
            row["id"] <= 0
            or row["quantity"] <= 0
            or row["amount"] is not None
            and row["amount"] < 0
            or not row["public_id"]
            or len(row["public_id"]) > 40
            or not row["draft_token"]
            or len(row["draft_token"]) > 128
        ):
            raise ValueError(f"invalid order {row['id']}")
        if row["delivery_error"] is not None and len(row["delivery_error"]) > 500:
            raise ValueError(f"invalid delivery error for order {row['id']}")
        if row["admin_telegram_id"] not in admin_ids:
            raise ValueError(f"order {row['id']} references unknown admin")
        if row["duplicate_of"] is not None and row["duplicate_of"] not in order_ids:
            raise ValueError(f"order {row['id']} references unknown duplicate")
        if row["delivery_status"] not in {
            "pending",
            "sending",
            "sent",
            "failed",
            "ambiguous",
        }:
            raise ValueError(f"invalid delivery status for order {row['id']}")
        if row["delivery_attempts"] is None or row["delivery_attempts"] < 0:
            raise ValueError(f"invalid delivery attempts for order {row['id']}")
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
    if values["delivery_status"] == "sending" or (
        values["delivery_status"] == "failed"
        and str(values["delivery_error"] or "").startswith("Ambiguous ")
    ):
        values["delivery_status"] = "ambiguous"
    return values


def outbox_values(row: dict[str, Any]) -> dict[str, Any] | None:
    status = immutable_order(row)["delivery_status"]
    if status == "sent":
        return None
    return {
        "status": "ambiguous" if status in {"sending", "ambiguous"} else status,
        "attempts": row["delivery_attempts"],
        # Outbox is an operational projection; keep the complete legacy error
        # only on orders.delivery_error and avoid copying possible PII here.
        "error_code": "legacy_delivery_error" if row["delivery_error"] else None,
    }


def differs(found: Any, expected: dict[str, Any], fields: tuple[str, ...]) -> bool:
    return any(found[field] != expected[field] for field in fields)


async def reset_sequences(connection: Any) -> None:
    for table in ("users", "orders", "outbox"):
        sequence = (
            await connection.execute(
                text("SELECT pg_get_serial_sequence(:table, 'id')"), {"table": table}
            )
        ).scalar_one()
        if sequence:
            await connection.execute(
                text(
                    f"SELECT setval(:sequence, COALESCE((SELECT max(id) FROM {table}), 1), true)"
                ),
                {"sequence": sequence},
            )


async def run(source: str, destination: str, dry_run: bool) -> int:
    admins, orders, source_hash = read_source(source)
    validate_source(admins, orders)
    engine = create_async_engine(destination)
    imported = 0
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            admin_users: dict[int, int] = {}
            deferred_duplicates: list[tuple[int, int]] = []
            next_user_id = (
                int(
                    (
                        await connection.execute(
                            text("SELECT COALESCE(max(id), 0) FROM users")
                        )
                    ).scalar_one()
                )
                + 1
            )
            next_outbox_id = (
                int(
                    (
                        await connection.execute(
                            text("SELECT COALESCE(max(id), 0) FROM outbox")
                        )
                    ).scalar_one()
                )
                + 1
            )
            for row in admins:
                expected = immutable_admin(row)
                found = (
                    (
                        await connection.execute(
                            text(
                                "SELECT telegram_id, name, admin_code, is_active, created_at FROM admins WHERE telegram_id=:id"
                            ),
                            {"id": row["telegram_id"]},
                        )
                    )
                    .mappings()
                    .first()
                )
                if found and any(
                    found[field] != expected[field] for field in ADMIN_FIELDS
                ):
                    raise ValueError(f"admin conflict {row['telegram_id']}")
                if not found:
                    await connection.execute(
                        Admin.__table__.insert().values(**expected)
                    )
                user = (
                    (
                        await connection.execute(
                            text(
                                "SELECT id, telegram_id, username, password_hash, is_active, role FROM users WHERE telegram_id=:id"
                            ),
                            {"id": row["telegram_id"]},
                        )
                    )
                    .mappings()
                    .first()
                )
                if user and (
                    bool(user["is_active"]) != bool(row["is_active"])
                    or user["role"] != "seller"
                    or user["password_hash"] != DISABLED_PASSWORD_HASH
                ):
                    raise ValueError(f"user mapping conflict {row['telegram_id']}")
                if not user:
                    username = f"legacy_{row['telegram_id']}"
                    occupied = (
                        await connection.execute(
                            text("SELECT 1 FROM users WHERE username=:username"),
                            {"username": username},
                        )
                    ).first()
                    if occupied:
                        raise ValueError(f"user mapping conflict {row['telegram_id']}")
                    user = {"id": next_user_id}
                    user = (
                        (
                            await connection.execute(
                                User.__table__.insert()
                                .values(
                                    id=next_user_id,
                                    telegram_id=row["telegram_id"],
                                    username=username,
                                    password_hash=DISABLED_PASSWORD_HASH,
                                    role="seller",
                                    is_active=bool(row["is_active"]),
                                    token_version=0,
                                )
                                .returning(User.id)
                            )
                        )
                        .mappings()
                        .first()
                    )
                    next_user_id += 1
                admin_users[row["telegram_id"]] = int(user["id"])
            for row in orders:
                expected = immutable_order(row)
                found = (
                    (
                        await connection.execute(
                            text(
                                "SELECT "
                                + ",".join(ORDER_FIELDS)
                                + ",created_by_id FROM orders WHERE id=:id"
                            ),
                            {"id": row["id"]},
                        )
                    )
                    .mappings()
                    .first()
                )
                if found:
                    if (
                        differs(found, expected, ORDER_FIELDS)
                        or found["created_by_id"]
                        != admin_users[row["admin_telegram_id"]]
                    ):
                        raise ValueError(f"order conflict {row['id']}")
                    expected_outbox = outbox_values(row)
                    existing_outbox = (
                        (
                            await connection.execute(
                                text(
                                    "SELECT status, attempts, error_code FROM outbox WHERE order_id=:id"
                                ),
                                {"id": row["id"]},
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if expected_outbox is None and existing_outbox is not None:
                        raise ValueError(f"outbox conflict {row['id']}")
                    if expected_outbox is not None and (
                        existing_outbox is None
                        or differs(
                            existing_outbox,
                            expected_outbox,
                            ("status", "attempts", "error_code"),
                        )
                    ):
                        raise ValueError(f"outbox conflict {row['id']}")
                    continue
                values = dict(expected)
                values["created_by_id"] = admin_users[row["admin_telegram_id"]]
                duplicate_of = values["duplicate_of"]
                # Allow a legacy duplicate to point at a later numeric ID;
                # the self-FK is filled after every order exists.
                values["duplicate_of"] = None
                await connection.execute(Order.__table__.insert().values(**values))
                if duplicate_of is not None:
                    deferred_duplicates.append((row["id"], duplicate_of))
                projected = outbox_values(row)
                if projected is not None:
                    await connection.execute(
                        Outbox.__table__.insert().values(
                            id=next_outbox_id, order_id=row["id"], **projected
                        )
                    )
                    next_outbox_id += 1
                imported += 1
            for order_id, duplicate_of in deferred_duplicates:
                await connection.execute(
                    text("UPDATE orders SET duplicate_of=:duplicate_of WHERE id=:id"),
                    {"id": order_id, "duplicate_of": duplicate_of},
                )
            if not dry_run:
                await reset_sequences(connection)
            if dry_run:
                await transaction.rollback()
                print(
                    f"source_sha256={source_hash} admins={len(admins)} orders={len(orders)} dry_run=true"
                )
                return 0
            await transaction.commit()
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
