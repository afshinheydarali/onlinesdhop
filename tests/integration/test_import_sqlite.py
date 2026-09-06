"""Real PostgreSQL acceptance tests for the legacy SQLite importer."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from backend.models import Order, Outbox, User
from scripts.import_sqlite import run

DB_URL = os.getenv("TEST_DATABASE_URL")

SCHEMA = """
CREATE TABLE admins (
 telegram_id INTEGER PRIMARY KEY, name TEXT NOT NULL, admin_code TEXT NOT NULL,
 is_active INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE orders (
 id INTEGER PRIMARY KEY, public_id TEXT NOT NULL, admin_telegram_id INTEGER NOT NULL,
 customer_name TEXT NOT NULL, phone_raw TEXT NOT NULL, phone_normalized TEXT NOT NULL,
 province TEXT NOT NULL, city TEXT NOT NULL, address TEXT NOT NULL, postal_code TEXT,
 product_raw TEXT NOT NULL, product_normalized TEXT NOT NULL, quantity INTEGER NOT NULL,
 amount INTEGER, notes TEXT, photo_file_id TEXT NOT NULL, duplicate_of INTEGER,
 draft_token TEXT NOT NULL, created_at TEXT NOT NULL, delivery_status TEXT NOT NULL,
 delivery_attempts INTEGER NOT NULL, delivery_error TEXT,
 channel_photo_message_id INTEGER, channel_text_message_id INTEGER, delivered_at TEXT
);
"""


class ImporterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not DB_URL:
            raise RuntimeError("TEST_DATABASE_URL is required")
        parsed = make_url(DB_URL)
        if parsed.host not in {"127.0.0.1", "localhost", "::1"} or not (
            parsed.database or ""
        ).endswith("_test"):
            raise RuntimeError("refusing to reset a non-test database")
        self.engine = create_async_engine(DB_URL)
        async with self.engine.begin() as c:
            await c.execute(
                text(
                    "TRUNCATE outbox, idempotency_keys, orders, admins, users RESTART IDENTITY CASCADE"
                )
            )
        self.temp = tempfile.TemporaryDirectory(prefix="import # ")
        self.source = str(Path(self.temp.name) / "legacy # snapshot.sqlite")
        c = sqlite3.connect(self.source)
        c.executescript(SCHEMA)
        c.execute(
            "INSERT INTO admins VALUES (?,?,?,?,?)",
            (7, "Inactive", "INACTIVE7", 0, "2026-01-01T00:00:00+00:00"),
        )
        rows = [
            (
                20,
                "P20",
                7,
                "N",
                "raw",
                "norm",
                "pr",
                "ci",
                "addr",
                None,
                "prod",
                "prod",
                1,
                None,
                None,
                "file",
                30,
                "t20",
                "2026-01-02T00:00:00+00:00",
                "pending",
                2,
                "err",
                None,
                None,
                None,
            ),
            (
                30,
                "P30",
                7,
                "N",
                "raw",
                "norm",
                "pr",
                "ci",
                "addr",
                None,
                "prod",
                "prod",
                1,
                5,
                None,
                "file",
                None,
                "t30",
                "2026-01-03T00:00:00+00:00",
                "sending",
                3,
                "uncertain",
                11,
                12,
                None,
            ),
            (
                40,
                "P40",
                7,
                "N",
                "raw",
                "norm",
                "pr",
                "ci",
                "addr",
                None,
                "prod",
                "prod",
                1,
                None,
                None,
                "file",
                None,
                "t40",
                "2026-01-04T00:00:00+00:00",
                "sent",
                1,
                None,
                21,
                22,
                "2026-01-04T00:00:00+00:00",
            ),
        ]
        c.executemany("INSERT INTO orders VALUES (" + ",".join("?" * 25) + ")", rows)
        c.commit()
        c.close()

    async def asyncTearDown(self):
        self.temp.cleanup()
        await self.engine.dispose()

    async def counts(self):
        async with self.engine.connect() as c:
            return {
                t: (await c.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one()
                for t in ("users", "admins", "orders", "outbox")
            }

    async def sequences(self):
        async with self.engine.connect() as c:
            return {
                table: (
                    await c.execute(
                        text(f"SELECT last_value, is_called FROM public.{table}_id_seq")
                    )
                ).one()
                for table in ("users", "orders", "outbox")
            }

    async def test_gapped_forward_duplicate_repeat_and_partial_delivery(self):
        before = hashlib.sha256(Path(self.source).read_bytes()).hexdigest()
        self.assertEqual(await run(self.source, DB_URL, False), 3)
        self.assertEqual(await run(self.source, DB_URL, False), 0)
        self.assertEqual(
            await self.counts(), {"users": 1, "admins": 1, "orders": 3, "outbox": 2}
        )
        self.assertEqual(
            hashlib.sha256(Path(self.source).read_bytes()).hexdigest(), before
        )
        async with self.engine.connect() as c:
            rows = (
                await c.execute(
                    text(
                        "SELECT id, duplicate_of, delivery_status FROM orders ORDER BY id"
                    )
                )
            ).all()
            self.assertEqual(
                rows, [(20, 30, "pending"), (30, None, "sending"), (40, None, "sent")]
            )
            self.assertEqual(
                (
                    await c.execute(
                        text("SELECT role,is_active FROM users WHERE telegram_id=7")
                    )
                ).one(),
                ("seller", False),
            )
            self.assertEqual(
                (
                    await c.execute(
                        text("SELECT status,error_code FROM outbox WHERE order_id=30")
                    )
                ).one(),
                ("ambiguous", "legacy_delivery_error"),
            )

    async def test_dry_run_rolls_back_and_invalid_source_rolls_back(self):
        self.assertEqual(await run(self.source, DB_URL, True), 0)
        self.assertEqual(
            await self.counts(), {"users": 0, "admins": 0, "orders": 0, "outbox": 0}
        )
        c = sqlite3.connect(self.source)
        c.execute("UPDATE orders SET amount=-1 WHERE id=20")
        c.commit()
        c.close()
        with self.assertRaises(ValueError):
            await run(self.source, DB_URL, True)
        self.assertEqual(
            await self.counts(), {"users": 0, "admins": 0, "orders": 0, "outbox": 0}
        )

    async def test_existing_changed_field_conflicts_atomically(self):
        await run(self.source, DB_URL, False)
        c = sqlite3.connect(self.source)
        c.execute("UPDATE orders SET public_id='CHANGED' WHERE id=20")
        c.commit()
        c.close()
        with self.assertRaises(ValueError):
            await run(self.source, DB_URL, False)
        self.assertEqual(
            await self.counts(), {"users": 1, "admins": 1, "orders": 3, "outbox": 2}
        )

    async def test_dry_run_enforces_unique_public_id(self):
        await run(self.source, DB_URL, False)
        c = sqlite3.connect(self.source)
        c.execute("DELETE FROM orders")
        c.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                50,
                "P20",
                7,
                "N",
                "raw",
                "norm",
                "pr",
                "ci",
                "addr",
                None,
                "prod",
                "prod",
                1,
                None,
                None,
                "file",
                None,
                "t50",
                "2026-01-05T00:00:00+00:00",
                "sent",
                1,
                None,
                21,
                22,
                "2026-01-05T00:00:00+00:00",
            ),
        )
        c.commit()
        c.close()
        with self.assertRaises(IntegrityError):
            await run(self.source, DB_URL, True)
        self.assertEqual(
            await self.counts(), {"users": 1, "admins": 1, "orders": 3, "outbox": 2}
        )

    async def test_dry_run_keeps_sequences_empty_and_nonempty_unchanged(self):
        empty_before = await self.sequences()
        await run(self.source, DB_URL, True)
        self.assertEqual(await self.sequences(), empty_before)
        await run(self.source, DB_URL, False)
        before = await self.sequences()
        c = sqlite3.connect(self.source)
        c.execute("DELETE FROM orders")
        c.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                60,
                "P60",
                7,
                "N",
                "raw",
                "norm",
                "pr",
                "ci",
                "addr",
                None,
                "prod",
                "prod",
                1,
                None,
                None,
                "file",
                None,
                "t60",
                "2026-01-05T00:00:00+00:00",
                "sent",
                1,
                None,
                21,
                22,
                "2026-01-05T00:00:00+00:00",
            ),
        )
        c.commit()
        c.close()
        await run(self.source, DB_URL, True)
        self.assertEqual(await self.sequences(), before)

    async def test_generated_ids_are_above_imported_maxima(self):
        await run(self.source, DB_URL, False)
        async with self.engine.begin() as c:
            user_id = (
                await c.execute(
                    User.__table__.insert()
                    .values(
                        telegram_id=900,
                        username="generated",
                        password_hash="x",
                        role="seller",
                        is_active=True,
                        token_version=0,
                    )
                    .returning(User.id)
                )
            ).scalar_one()
            order_id = (
                await c.execute(
                    Order.__table__.insert()
                    .values(
                        public_id="generated-order",
                        admin_telegram_id=7,
                        created_by_id=user_id,
                        customer_name="N",
                        phone_raw="r",
                        phone_normalized="n2",
                        province="p",
                        city="c",
                        address="a",
                        product_raw="x",
                        product_normalized="x",
                        quantity=1,
                        photo_file_id="f",
                        draft_token="generated-token",
                        created_at=datetime(2026, 1, 5, tzinfo=UTC),
                    )
                    .returning(Order.id)
                )
            ).scalar_one()
            outbox_id = (
                await c.execute(
                    Outbox.__table__.insert()
                    .values(order_id=order_id)
                    .returning(Outbox.id)
                )
            ).scalar_one()
        self.assertGreater(user_id, 1)
        self.assertGreater(order_id, 40)
        self.assertGreater(outbox_id, 2)


if __name__ == "__main__":
    unittest.main()
